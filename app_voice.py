
"""Voice p2p engine (tay build order item 3): the hub is the directory.

The roster (join/leave/mute flags/peer voice-destination hashes) rides
the guild push path; audio frames ride DIRECT peer links:
speaker -> listener RNS.Link packets, no hub relay, no app-level forward
on the hub. This module is kivy-free so the bench can drive it.

Structure:
  VoiceEngine   one client's voice stack: its own hades/voice inbound
                destination (speakers accept listener links), outbound
                links to each roster peer (listener side), the encode
                pacer (N frames per outbound packet per listener), the
                per-speaker jitter buffer + PLC decoder feeding a sink.
Transport is injected (send_packet/receive hooks) so the unit bench can
run the whole loop with a fake and the client with real RNS.
"""

import os
import threading
import time
import struct

import RNS

VOICE_DEBUG = os.environ.get("SHADES_VOICE_DEBUG") == "1"

# a peer still undialed after this many attempts rides the hub relay
# unless or until a roster push re-lists it and a dial succeeds
DIAL_MAX_TRIES = 8

try:
    import voice_codec as vc
except Exception:  # pragma: no cover - keeps lint import paths sane
    vc = None

PACKET_FRAMES = 5          # 5 x 20ms = 100ms per outbound packet
PACKET_INTERVAL_S = 0.1
JITTER_MAX_PACKETS = 6     # per-speaker holdback (~600ms ceiling)

# tay 2026-11-25: transmit shaping. "always" = open mic while
# unmuted; "ptt" = transmit only while a hold control is down;
# "vad" (voc) = transmit when the mic frame's RMS crosses the
# threshold, bridged by a 200ms hangover so speech doesn't chop.
VOICE_MODES = ("always", "ptt", "vad")
VAD_RMS_DEFAULT = 800      # ~ -32 dBFS on 16-bit mono
VAD_HANGOVER_FRAMES = 10
SPEAK_WINDOW_S = 0.35      # packet age within this = visibly speaking


def frame_rms_s16(pcm):
    """Root mean square of a little-endian 16-bit mono frame, or 0."""
    n = len(pcm) // 2
    if not n:
        return 0
    samples = struct.unpack("<%dh" % n, pcm[:n * 2])
    acc = sum(s * s for s in samples)
    return int((acc / n) ** 0.5)


class FakeLink(object):
    """Unit-test stand-in for an RNS.Link. Records sends, optionally
    drops whole packets (loss lane), and feeds receive through the
    engine's demux exactly like the real packet callback would."""

    def __init__(self, engine, peer, loss=0.0):
        self.engine = engine
        self.peer = peer
        self.loss = loss
        self.sends = 0
        self.dropped = 0
        self.alive = True

    def send(self, body):
        import random
        if not self.alive or random.random() < self.loss:
            self.dropped += 1
            return
        self.sends += 1
        self.engine._deliver(self.peer, body)

    def teardown(self):
        self.alive = False


class VoiceEngine(object):
    """One client in one voice room. kivy-free; wires are injected.

    identity      the client's RNS.Identity (destination announced by
                  the wiring layer; the bench runs without RNS)
    hub           metadata-only hub face: join(cid, dest, muted),
                  leave(), and nothing else -- no frame ever goes here
    viewer        this client's roster hash (lxmf delivery hash)
    """

    def __init__(self, identity=None, viewer=None, hub=None):
        if vc is None:
            raise RuntimeError("voice_codec missing")
        self.codec = vc.VoiceCodec()
        self.identity = identity
        self.viewer = viewer
        self.hub = hub
        self.room_cid = None
        self.muted = False
        self._dest_hex = None
        self._source = None       # cb -> 960-sample pcm bytes (or None)
        self._sink = None         # cb(pcm bytes)
        self._send = None         # cb(peer_dest_hex, body) transport hook
        self._verify_member = None  # cb(identity_or_viewer) -> bool
        self._out_links = {}      # peer dest hex -> FakeLink-like
        self._in_links = {}       # listeners' inbound links (speakers)
        self._jitter = {}         # speaker dest hex -> packet deque
        self._speakers_pinned = set()
        self._link_factory = None  # cb(peer_hex) -> link-like (blocking)
        self._dial_q = []         # peers queued for the dialer thread
        self._dial_failures = {}  # peer hex -> failed dial count
        self._dialer = None
        self._dialing = threading.Event()
        self._pacer = None
        self._player = None
        self._pacing = threading.Event()
        self._playing = threading.Event()
        self._out_seq = 0
        self._relay_pubs = 0
        self.frames_in = 0
        self.frames_out = 0
        self.plc_frames = 0
        self.relayed_frames = 0   # hub forwards: never moves (receipt)
        self.last_played_at = 0.0
        # tx shaping (tay 2026-11-25: push-to-talk / always-on / voc)
        self.voice_mode = "always"
        self.vad_rms = VAD_RMS_DEFAULT
        self._ptt_down = False
        self._vad_hang = 0
        self._tx_live = False
        self._last_rx = {}        # speaker dest hex -> last packet time

    # ---- wiring -------------------------------------------------------

    def attach_source(self, cb):
        self._source = cb

    def attach_sink(self, cb):
        self._sink = cb

    def attach_transport(self, send_cb):
        """send_cb(peer_dest_hex, body): the real layer pushes the packet
        down an open RNS.Link to that peer; the bench's FakeTransport."""
        self._send = send_cb

    def attach_verifier(self, cb):
        """cb(viewer_hash) -> True if that member is in the roster now."""
        self._verify_member = cb

    def dest_hex(self):
        return self._dest_hex

    def set_dest(self, dest_hex):
        self._dest_hex = dest_hex

    # ---- room lifecycle -----------------------------------------------

    def join(self, cid, muted=False):
        """Join the room: announce self, register with the hub (metadata
        only), start pacing + playback loops."""
        if self.hub is not None:
            r = self.hub.join(cid, self._dest_hex, muted)
            if not r or not r.get("ok"):
                return r
            self.room_cid = cid
            self.muted = muted
            self.start()
            # the join response carries the room's live roster: callers
            # seed their engines from it (push events only cover the
            # post-subscribe window)
            r["room"] = cid
            return r
        self.room_cid = cid
        self.muted = muted
        self.start()
        return {"ok": 1, "room": cid}

    def leave(self):
        self.stop()
        cid = self.room_cid
        self.room_cid = None
        self._out_links.clear()
        self._jitter.clear()
        if self.hub is not None:
            self.hub.leave()
        return {"ok": 1, "left": cid}

    def set_muted(self, muted):
        self.muted = muted
        return muted

    # ---- tx shaping (tay 2026-11-25) ------------------------------------

    def set_voice_mode(self, mode):
        """always | ptt | vad (voc). Anything else keeps the current."""
        if mode in VOICE_MODES:
            self.voice_mode = mode
        self._vad_hang = 0
        return self.voice_mode

    def set_ptt(self, down):
        """PTT hold state down/up (one control may call this per tick)."""
        self._ptt_down = bool(down)
        return self._ptt_down

    def speaking_peers(self):
        """Dest hexes with a voice packet inside SPEAK_WINDOW_S."""
        now0 = time.time()
        last = dict(self._last_rx)
        return [d for d, t in last.items() if now0 - t <= SPEAK_WINDOW_S]

    def self_speaking(self):
        return bool(self._tx_live) and not self.muted

    # ---- roster (push path) --------------------------------------------

    def on_roster(self, cid, members):
        """From the room's voice_state push. Peers = roster minus self,
        each with its own declared voice destination. Dialing runs on the
        dialer thread (path discovery can block); this stays O(1)."""
        if cid != self.room_cid:
            return
        alive = set()
        for m in members:
            if m.get("viewer") == self.viewer or not m.get("dest"):
                continue
            dest = m["dest"]
            alive.add(dest)
            if dest not in self._out_links and dest not in self._dial_q:
                if self._link_factory is not None:
                    self._dial_q.append(dest)
                else:
                    # bench lane: a fake link stands in for the dialer
                    link = FakeLink(self, dest)
                    self._out_links[dest] = link
                    self._jitter.setdefault(
                        dest, {"buf": [], "last": time.time()})
        for dest in list(self._out_links):
            if dest not in alive:
                self._out_links.pop(dest).teardown()
                self._jitter.pop(dest, None)
        # a departed peer's dial-failure count outlives it pointlessly;
        # if they rejoin, the dial starts fresh
        for dest in list(self._dial_failures):
            if dest not in alive:
                self._dial_failures.pop(dest, None)
        # pinned speakers: expected-active peers, so playback gaps in
        # their streams raise PLC frames instead of bare silence
        self._speakers_pinned = alive.copy()
        if VOICE_DEBUG:
            print("roster %s: pinned=%s queued=%s" % (
                cid, self._speakers_pinned, self._dial_q), flush=True)

    # ---- speaker side ---------------------------------------------------

    def _pacer_loop(self):
        while not self._pacing.wait(PACKET_INTERVAL_S):
            t_tick = time.time()
            if self.room_cid is None or self.muted or self._source is None:
                self._vad_hang = 0
                self._tx_live = False
                continue
            if self.voice_mode == "ptt" and not self._ptt_down:
                self._tx_live = False
                continue
            frames = []
            for _ in range(PACKET_FRAMES):
                pcm = self._source()
                if not pcm:
                    continue
                if self.voice_mode == "vad":
                    loud = frame_rms_s16(pcm) >= self.vad_rms
                    if loud:
                        self._vad_hang = VAD_HANGOVER_FRAMES
                    elif self._vad_hang <= 0:
                        continue
                    else:
                        self._vad_hang -= 1
                frames.append(self.codec.encode(pcm))
                self.frames_out += 1
            self._tx_live = bool(frames)
            if not frames:
                continue
            body = vc.encode_packet(self.codec, frames)
            body = self._out_seq.to_bytes(2, "big") + body
            self._out_seq = (self._out_seq + 1) & 0xFFFF
            # a peer linked both ways (out dial + inbound dial) receives
            # one packet, not two
            targets = dict(self._in_links)
            targets.update(self._out_links)
            for peer, link in list(targets.items()):
                try:
                    if not link.alive:
                        continue
                    self._send(peer, body) if self._send else link.send(body)
                except Exception:
                    pass
            if VOICE_DEBUG and self.frames_out % 250 == 0:
                dt = time.time() - t_tick
                print("pacer state: pinned=%s fails=%s dial_q=%s "
                      "targets=%s tick_dt=%.3f" % (
                          sorted(self._speakers_pinned)[:1],
                          dict(self._dial_failures),
                          list(self._dial_q)[:2],
                          list(targets)[:2], dt), flush=True)
            # probe-failed pairs: one relay publish covers every roster
            # peer we could never link. The hub tags the frame with our
            # voice destination and fans it out itself.
            if self._relay_peers(targets):
                pub = getattr(self.hub, "voice_publish", None)
                if pub is not None:
                    try:
                        pub(self.room_cid, body)
                        if VOICE_DEBUG:
                            self._relay_pubs += 1
                            print("relay publish n=%d peers=%s" % (
                                self._relay_pubs,
                                self._relay_peers(targets)), flush=True)
                    except Exception:
                        pass

    def _relay_peers(self, targets):
        """Roster-active peers with no link, no pending dial, and a
        dial-failure count at the cap: their only route is the relay."""
        return [d for d in self._speakers_pinned
                if d not in targets and d not in self._dial_q
                and self._dial_failures.get(d, 0) >= DIAL_MAX_TRIES]

    # ---- listener side --------------------------------------------------

    def packet_received(self, speaker_dest_hex, body):
        """Demux: unpack, hold in the per-speaker jitter, feed the pump."""
        self._last_rx[speaker_dest_hex] = time.time()
        if len(body) < 2:
            return
        seq = int.from_bytes(body[:2], "big")
        try:
            frames = vc.decode_packet(body[2:])
        except ValueError:
            if VOICE_DEBUG:
                print("demux %s: decode rejected %d bytes %s..." % (
                    speaker_dest_hex[:8], len(body),
                    (body[2:12] or b"").hex()), flush=True)
            return
        j = self._jitter.get(speaker_dest_hex)
        if j is None:
            if VOICE_DEBUG:
                print("demux %s: no jitter, keys=%s" % (
                    speaker_dest_hex[:8],
                    [k[:8] for k in self._jitter]), flush=True)
            return
        j["buf"].append((seq, time.time(), frames))
        if len(j["buf"]) > JITTER_MAX_PACKETS:
            j["buf"].pop(0)   # a clogged peer loses its oldest packet
        self.frames_in += len(frames)

    def packet_relayed(self, speaker_dest_hex, body):
        """Hub-relayed voice entry point (the "voice" push frame). A
        speaker whose stream arrives directly is not fed twice: relayed
        copies of a directly linked speaker are dropped here, direct
        wins."""
        if speaker_dest_hex in self._out_links or \
                speaker_dest_hex in self._in_links:
            return
        # a dialed speaker builds its jitter on link-up; a relayed-only
        # speaker (undialable pair, hub fallback) has no buffer yet, so
        # seed one here or the frame is dropped in packet_received
        self._jitter.setdefault(
            speaker_dest_hex, {"buf": [], "last": time.time()})
        return self.packet_received(speaker_dest_hex, body)

    # ---- dialer (listener connects to each roster peer) -----------------

    def set_link_factory(self, cb):
        """cb(peer_dest_hex) -> link-like (alive/send/teardown) or None.
        Blocking calls live on the dialer thread, never the roster demux."""
        if cb is not None:
            self._link_factory = cb

    def accept_inbound(self, peer_dest_hex, link):
        """A listener dialed this engine's voice destination: register the
        inbound link for fan-out. The transport verifies roster membership
        BEFORE calling; the engine trusts that gate."""
        if peer_dest_hex and link is not None:
            self._in_links[peer_dest_hex] = link
            return True
        return False

    def _dialer_loop(self):
        while not self._dialing.wait(0.05):
            if not self._dial_q:
                continue
            dest = self._dial_q.pop(0)
            if self.room_cid is None or self._link_factory is None:
                continue
            try:
                link = self._link_factory(dest)
            except Exception as e:
                link = None
                print("voice dial %s failed: %r" % (dest[:8], e))
            if link is None:
                # retried on a backoff, capped: a permanently failed pair
                # stops dialing until the next roster push re-lists it
                self._dial_failures[dest] = self._dial_failures.get(dest, 0) + 1
                if self._dial_failures[dest] <= DIAL_MAX_TRIES:
                    self._dialing.wait(1.0)
                    self._dial_q.append(dest)
                continue
            self._dial_failures.pop(dest, None)
            if dest not in self._speakers_pinned:
                try:
                    link.teardown()
                except Exception:
                    pass
                continue    # the roster moved on while we dialed
            self._out_links[dest] = link
            self._jitter.setdefault(dest, {"buf": [], "last": time.time()})

    def _deliver(self, peer, body):
        """FakeLink's receive side; the real layer routes packets on its
        inbound links here."""
        self.packet_received(peer, body)

    def _player_loop(self):
        while not self._playing.wait(0.02):
            if self.room_cid is None or self._sink is None:
                continue
            now = time.time()
            for dest, j in list(self._jitter.items()):
                buf = j["buf"]
                if buf:
                    seq, at, frames = buf[0]
                    if now - at < 0.06:
                        continue   # holdback window stays a window
                    buf.pop(0)
                    for frame in frames:
                        try:
                            self._sink(self.codec.decode(frame))
                        except Exception:
                            pass
                # gap: the speaker had no fresh packet -> PLC frame
                if not buf and self._speakers_pinned and \
                        now - j["last"] > 0.12:
                    try:
                        self._sink(self.codec.decode(None))
                        self.plc_frames += 1
                    except Exception:
                        pass
                    j["last"] = now
                elif buf:
                    j["last"] = now

    # ---- loops -----------------------------------------------------------

    def start(self):
        if self._pacer is None:
            self._pacing = threading.Event()
            self._pacer = threading.Thread(
                target=self._pacer_loop, daemon=True, name="voice-pacer")
            self._pacer.start()
        if self._player is None:
            self._playing = threading.Event()
            self._player = threading.Thread(
                target=self._player_loop, daemon=True, name="voice-player")
            self._player.start()
        if self._link_factory is not None and self._dialer is None:
            self._dialing = threading.Event()
            self._dialer = threading.Thread(
                target=self._dialer_loop, daemon=True, name="voice-dialer")
            self._dialer.start()

    def stop(self):
        self._out_seq = 0
        self._in_links.clear()
        if self._pacer is not None:
            self._pacing.set()
            self._pacer = None
        if self._player is not None:
            self._playing.set()
            self._player = None
        if self._dialer is not None:
            self._dialing.set()
            self._dialer = None
            self._dial_q = []


class RNSLinkProxy(object):
    """One established RNS.Link, shaped like the engine's FakeLink."""
    __slots__ = ("link", "peer", "engine")

    def __init__(self, engine, peer_hex, link):
        self.engine = engine
        self.peer = peer_hex
        self.link = link

    @property
    def alive(self):
        return self.link.status == RNS.Link.ACTIVE

    def send(self, body):
        # this RNS version carries link frames as packets
        # (the hub's own push path at sHades.py:1741 does the same)
        return RNS.Packet(self.link, body).send()

    def teardown(self):
        try:
            self.link.teardown()
        except Exception:
            pass


class RNSVoiceTransport(object):
    """RNS-backed transport: the engine's own hades/voice inbound
    destination (speaker accepts listener links against the roster) and
    outbound dials to each roster peer's voice destination."""

    def __init__(self, engine, identity, verifier=None, wait_secs=20.0):
        self.engine = engine
        self.identity = identity
        self.verifier = verifier       # cb(viewer_hex) -> bool
        self.wait_secs = wait_secs
        self.dest = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            "hades", "voice")
        self.dest_hash_hex = RNS.hexrep(self.dest.hash, delimit=False)
        engine.set_dest(self.dest_hash_hex)
        self.dest.announce()           # mesh-wide discovery for listeners
        self.dest.set_link_established_callback(self._on_in_link)
        engine.set_link_factory(self.dial)

    @staticmethod
    def viewer_hash_of_identity(nid):
        """The roster's viewer hashes are lxmf delivery hashes."""
        return RNS.hexrep(
            RNS.Destination(
                nid, RNS.Destination.OUT, RNS.Destination.SINGLE,
                "lxmf", "delivery").hash, delimit=False)

    @classmethod
    def voice_hash_of_identity(cls, nid):
        return RNS.hexrep(
            RNS.Destination(
                nid, RNS.Destination.OUT, RNS.Destination.SINGLE,
                "hades", "voice").hash, delimit=False)

    def _on_in_link(self, link):
        """Inbound listener link. Verify against the roster; refuse
        everyone else. The caller's identity is known only after it
        identifies; until then acceptance waits on the identified
        callback (get_remote_identity is None before that)."""
        nid = link.get_remote_identity()
        if nid is None and not link.initiator:
            link.set_remote_identified_callback(self._on_identified)
            return
        self._accept(link, nid)

    def _on_identified(self, link, nid):
        self._accept(link, nid)

    def _accept(self, link, nid):
        accepted = None
        if nid is not None:
            vhash = self.viewer_hash_of_identity(nid)
            if self.verifier is None or self.verifier(vhash):
                vdest = self.voice_hash_of_identity(nid)
                accepted = vdest
                link.set_packet_callback(
                    lambda plaintext, packet=None, v=vdest:
                    self.engine.packet_received(v, plaintext))
                self.engine.accept_inbound(vdest, RNSLinkProxy(
                    self.engine, vdest, link))
        if accepted is None:
            try:
                link.teardown()
            except Exception:
                pass

    def dial(self, peer_hex):
        """Blocking dial to one roster peer (dialer thread + bench)."""
        try:
            h = bytes.fromhex(peer_hex)
        except ValueError:
            return None
        deadline = time.time() + self.wait_secs
        nid = RNS.Identity.recall(h)
        if VOICE_DEBUG:
            print("voice dial %s: recall-now=%s path-now=%s (t=%.1f)" % (
                peer_hex[:12], nid is not None,
                RNS.Transport.has_path(h), time.time() % 1000.0), flush=True)
        while nid is None and time.time() < deadline:
            RNS.Transport.request_path(h)
            time.sleep(0.4)
            nid = RNS.Identity.recall(h)
        if nid is None:
            print("voice dial %s: identity never announced in time "
                  "(no recall)" % peer_hex[:12])
            return None    # probe-failed pair; relay fallback is later
        peer_dest = RNS.Destination(
            nid, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "hades", "voice")
        if not RNS.Transport.has_path(peer_dest.hash):
            RNS.Transport.request_path(peer_dest.hash)
            while not RNS.Transport.has_path(peer_dest.hash):
                if time.time() > deadline:
                    print("voice dial %s: no transport path in time"
                          % peer_hex[:12])
                    return None
                time.sleep(0.4)
        link = RNS.Link(peer_dest)
        dead = {s for s in (getattr(RNS.Link, "CLOSED", None),
                            getattr(RNS.Link, "FAILED", None))
                if s is not None}
        while link.status != RNS.Link.ACTIVE:
            if time.time() > deadline or link.status in dead:
                try:
                    link.teardown()
                except Exception:
                    pass
                return None
            time.sleep(0.1)
        link.identify(self.identity)
        link.set_packet_callback(
            lambda plaintext, packet=None, v=peer_hex:
            self.engine.packet_received(v, plaintext))
        return RNSLinkProxy(self.engine, peer_hex, link)
