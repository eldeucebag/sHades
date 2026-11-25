"""Transport core for the voice lane: RNS-side pieces the panel's
engine setup needs on every platform, without a desktop audio binding.

Split from client_voice when the device re-land hit the layering
defect: the panel's setup imported the desktop module unconditionally
for the lane, and client_voice's top-level ``import pyaudio`` made
every device boot fail at join before any mic/sink choice (receipt:
voice engine setup failed: ModuleNotFoundError("No module named
'pyaudio'"), six attempts on the voice join). The engine contracts are
unchanged: the panel imports this core for VoiceLane/RNSLinkShell/bind
on both platforms, then picks the device layer per platform --
client_voice.MicSource/SpeakerSink on desktop, client_voice_android's
AndroidMic/AndroidSpeaker over jnius AudioRecord on the device.

No device: desktop constructors raise VoiceDeviceError with the
reason, and the android layer raises it when jnius lacks the audio
classes, so the panel can present listen-only instead of half-wired.
"""
# must equal app_voice.AUDIO_RATE / PACKET_FRAMES pacing; 20ms frames
FRAME_MS = 20
# Frame geometry rides voice_codec, not the mic rate: the engine's
# source contract is one codec-rate frame per poll (960 samples at
# 48 kHz, 1920 bytes). The first cut used 16 kHz mic defaults and a
# 640-byte frame, which every codec.encode call would have rejected;
# the device gate's sine loopback never caught it because its source
# generates 48 kHz frames itself.
FRAMES_PER_CHUNK = 960          # 48 kHz * 0.02 s (bytes: *2, s16 mono)
FRAME_BYTES = FRAMES_PER_CHUNK * 2


class VoiceDeviceError(Exception):
    pass


def bind(engine, mic=None, speaker=None):
    """Wire source/sink callbacks into a VoiceEngine. Either side may
    be omitted (a listen-only seat passes mic=None; a speaker seat that
    stays muted never calls the sink)."""
    if mic is not None:
        engine.attach_source(mic.poll)
    if speaker is not None:
        engine.attach_sink(speaker)
    return mic, speaker


class RNSLinkShell:
    """The VoiceEngine's link-like view of a live RNS.Link. alive reads
    the link status so pre-establish sends are skipped; send pushes the
    engine's raw packet down the link."""

    def __init__(self, link):
        self._link = link
        self.remote_dest = None

    @property
    def alive(self):
        import RNS
        try:
            return getattr(self._link, "status", None) == RNS.Link.ACTIVE
        except Exception:
            return False

    def send(self, body):
        self._link.send(body)

    def teardown(self):
        try:
            self._link.teardown()
        except Exception:
            pass


class VoiceLane:
    """One client's RNS voice lane: an announced listening destination
    plus a dial factory, both feeding one VoiceEngine."""

    APP_NAME = "shades"
    ASPECT = "voice"

    def __init__(self, identity):
        self.identity = identity
        self._listener = None
        self._dest = None
        self.engine = None

    def open_listener(self, engine, announce=True):
        """IN destination on the client identity; raw voice packets from
        any peer's link feed engine.accept_inbound."""
        import RNS
        self.engine = engine
        self._dest = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            self.APP_NAME, self.ASPECT)

        def on_link(link):
            shell = RNSLinkShell(link)
            try:
                ri = link.get_remote_identity()
                if ri is not None:
                    shell.remote_dest = RNS.Destination.hash(ri).hex()
                    engine.accept_inbound(shell.remote_dest, shell)
            except Exception:
                pass

        self._dest.set_link_established_callback(on_link)
        if announce:
            self._dest.announce(app_data=self._dest.hash)
        return self._dest.hash.hex() if hasattr(self._dest.hash, "hex") \
            else self._dest.hash

    def close_listener(self):
        try:
            self._dest.teardown()
        except Exception:
            pass
        self._dest = None
        self._listener = None

    def dial_factory(self, engine):
        def make(dest_hex):
            import RNS
            if not dest_hex:
                return None
            dest_bytes = bytes.fromhex(dest_hex)
            ri = RNS.Identity.recall(dest_bytes)
            if ri is None:
                # path not heard yet: the roster's dest hashes rely on
                # peers' voice announces; skip this tick, retry on the
                # next roster push
                return None
            out = RNS.Destination(ri, RNS.Destination.OUT,
                                  RNS.Destination.SINGLE,
                                  self.APP_NAME, self.ASPECT)
            link = RNS.Link(out)
            shell = RNSLinkShell(link)
            return shell
        return make
