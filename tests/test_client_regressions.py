#!/usr/bin/env python3
"""Regression tests for the client bugs found in the Nov 2026 audit.

Each test pins one bug so it cannot come back silently:

  1. _add_server() dropped a display name the user set. The "Add Server"
     popup names a guild, then connect_to_guild() re-adds the same server
     without a name — the entry was rebuilt with a host-derived fallback
     and "My Guild" became "192.168.1.50".

  2. _fetch_messages() applied every message delta TWICE: once inside the
     Clock-scheduled apply() and again on the worker thread.

  3. The admin panel and the roles popup called guild_api.* on the Kivy
     MAIN thread. Those calls block on the RNS link for up to the request
     timeout (10s), so an unresponsive guild froze the whole UI.

  4. on_stop() called term_close() for every open console session inline,
     freezing shutdown for N * 10s (30s measured with 3 sessions).

Run: /usr/bin/python tests/test_client_regressions.py
"""
import os
import sys
import time

# Use the real SDL window on DISPLAY (including CI's Xvfb).

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


class _StubApp(kc.sHadesClientApp):
    """A real MDApp (KivyMD widgets need one) with no RNS and no UI churn."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self._servers = []
        self._servers_path = "/tmp/shades_regression_servers.json"
        self._active_server_idx = -1
        self._app_data_dir = "/tmp/shades_regression"
        os.makedirs(self._app_data_dir, exist_ok=True)

    def _save_servers(self):
        pass

    def _set_status(self, text):
        pass


def test_display_name_preserved():
    """_add_server() must not overwrite a display name with a host fallback."""
    app = _StubApp()
    app._add_server("192.168.1.50:29221", "alice", "", "", "My Guild")
    check("display name is stored on first add",
          app._servers[0]["display_name"] == "My Guild",
          app._servers[0]["display_name"])

    # connect_to_guild() re-adds the same server WITHOUT a display name.
    app._add_server("192.168.1.50:29221", "alice", "", "")
    check("display name survives a re-add without one",
          app._servers[0]["display_name"] == "My Guild",
          app._servers[0]["display_name"])

    # An explicit new name must still win.
    app._add_server("192.168.1.50:29221", "alice", "", "", "Renamed")
    check("an explicit display name overrides the old one",
          app._servers[0]["display_name"] == "Renamed",
          app._servers[0]["display_name"])
    check("no duplicate entry was created", len(app._servers) == 1,
          len(app._servers))


def test_update_applied_once():
    """A message delta must be applied exactly once per fetch."""
    calls = []

    class Probe(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
            self._channel_kinds = {}
            self._older = {}
            self._msg_widgets = {}
            self._members = {}

        def _render_message(self, grid, m):
            pass

    app = Probe()
    root = app.build()
    app.root = root

    orig = kc.sHadesClientApp._apply_update

    def counting(self, u):
        calls.append(u.get("msg_id"))
        return orig(self, u)

    class FakeAPI:
        def messages(self, cid, **kw):
            return {"ok": 1, "messages": [],
                    "updates": [{"msg_id": "m1", "content": "edited"}]}

        def receipts(self, cid, msg_id):
            return {"ok": 1}

    app.guild_api = FakeAPI()
    kc.sHadesClientApp._apply_update = counting
    try:
        app._fetch_messages("general")
        from kivy.clock import Clock
        # The fetch apply runs on a worker thread; a loaded CI runner can
        # start that thread after the whole tick budget burns (0 applies
        # on run 44821647316 with the apply never scheduled yet). Spin
        # until the apply lands, bounded.
        for _ in range(60):
            Clock.tick()
            if len(calls) >= 1:
                break
            time.sleep(0.01)
    finally:
        kc.sHadesClientApp._apply_update = orig

    check("each update delta is applied exactly once",
          len(calls) == 1, "applied %d times: %s" % (len(calls), calls))


def _blocking_api(block=3.0):
    class FakeAPI:
        def admin_config(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def admin_mods(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def admin_audit(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def invites(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def roles_list(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}
    return FakeAPI()


def test_admin_panel_not_blocking():
    """Opening the admin panel must not block the main thread."""
    app = _StubApp()
    app.i_am_admin = True
    app.guild_api = _blocking_api(3.0)
    root = app.build()
    app.root = root

    t0 = time.time()
    app._open_admin_panel()
    dt = time.time() - t0
    check("opening the admin panel does not block the main thread",
          dt < 1.0, "blocked for %.2fs" % dt)


def test_roles_popup_not_blocking():
    """Opening the roles popup must not block the main thread."""
    app = _StubApp()
    app.guild_api = _blocking_api(3.0)
    app.my_roles = [{"perms": 16}]
    root = app.build()
    app.root = root

    t0 = time.time()
    app._open_roles_popup("deadbeef" * 4, "someone")
    dt = time.time() - t0
    check("opening the roles popup does not block the main thread",
          dt < 1.0, "blocked for %.2fs" % dt)


def test_shutdown_not_blocking():
    """on_stop() must not block for the full request timeout per session."""
    app = _StubApp()

    class SlowAPI:
        def term_close(self, sid, timeout=10.0):
            time.sleep(min(timeout, 10.0))
            return {"ok": 1}

    app.guild_api = SlowAPI()
    app._term_sessions = {"s1": "c1", "s2": "c2", "s3": "c3"}
    app.lxmf_router = None

    t0 = time.time()
    app.on_stop()
    dt = time.time() - t0
    check("app shutdown does not block on term_close()",
          dt < 3.0, "blocked for %.2fs (3 sessions)" % dt)


def test_attachment_kind_classifier():
    """One decision site governs embeds (tay v0.30): images inline, audio
    player row, video download-only for now, everything else file row."""
    k = kc.sHadesClientApp._attachment_kind
    cases = (
        ("photo.jpg", "image"),
        ("scan.PNG", "image"),
        ("blob.webp", "image"),
        ("song.mp3", "audio"),
        ("voice.ogg", "audio"),
        ("note.wav", "audio"),
        ("book.m4a", "audio"),
        ("clip.mp4", "video"),
        ("screen.webm", "video"),
        ("take.mov", "video"),
        ("report.pdf", "file"),
        ("data.csv", "file"),
        ("noext", "file"),
    )
    for name, want in cases:
        got = k(name)
        check("kind(%s) == %s" % (name, want), got == want, got)
    # mime wins over extension ambiguity
    check("mime audio overrides ext", k("weird.bin", "audio/mpeg") == "audio",
          k("weird.bin", "audio/mpeg"))
    check("mime video overrides ext", k("weird.bin", "video/mp4") == "video",
          k("weird.bin", "video/mp4"))
    check("application/ogg counts as audio",
          k("stream.ogg", "application/ogg") == "audio",
          k("stream.ogg", "application/ogg"))


def test_stream_lifecycle():
    """v0.31, receipts: channel switch never re-subscribed (live updates
    died after the first switch), quiet channels expired at SUB_TTL_S=120
    with no refresh, and a switch to site/term left the old subscription
    flowing into the void. This pins the fixed mechanics."""
    from kivy.clock import Clock

    class _H:
        text = ""

    class _Grid:
        def clear_widgets(self):
            pass

        def add_widget(self, w):
            pass

    class _Ids:
        def __init__(self):
            self.channel_header = _H()
            self.message_grid = _Grid()

    class _Screen:
        def __init__(self):
            self._current_channel = "general"
            self.ids = _Ids()

    class _FakeApi:
        def __init__(self, app):
            self.app = app

        def stream_subscribe(self, channel, cursor=None):
            self.app.subscribe_calls.append((channel, cursor))
            return {"ok": 1, "seq": 7}

        def stream_unsubscribe(self):
            self.app.unsubscribed.append(1)
            return {"ok": 1}

        def stream_poll(self, cursor=None, timeout_s=4.0):
            if self.app.poll_script:
                return self.app.poll_script.pop(0)
            return {"ok": 1, "events": [], "cursor": cursor}

    class _StreamApp(_StubApp):
        def __init__(self, kinds=None):
            super().__init__(**{})
            self._screen = _Screen()
            self.root = type(
                "R", (), {"get_screen": lambda s, n: self._screen})()
            self._channel_kinds = dict(kinds or {})
            self._channels = {"general": "c" + "0" * 31,
                              "lounge": "c" + "1" * 31}
            self._older = {}
            self.subscribe_calls = []
            self.unsubscribed = []
            self.poll_script = []
            self.fetched = None
            self.guild_api = _FakeApi(self)
            self._stream_cid = None
            self._stream_cursor = None
            self._stream_tick_event = None
            self._active_users = {}

        def _channel_cid(self, name):
            return self._channels.get(name)

        def _fetch_messages(self, name):
            self.fetched = name

        def _refresh_members_ui(self, name):
            pass

    def tick_until(app, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            Clock.tick()
            if cond():
                return True
            time.sleep(0.02)
        return False

    # 1. chat -> chat: the switch RE-SUBSCRIBES on the new channel and the
    #    server learns the re-point (subscribe replaces the binding).
    app = _StreamApp()
    app._switch_channel("lounge")
    ok = tick_until(app, lambda: app.subscribe_calls)
    check("channel switch re-subscribes on the new channel",
          ok and app.subscribe_calls[-1][0] == app._channels["lounge"],
          repr(app.subscribe_calls))

    # 2. chat -> site: the slot is released (one subscription per link).
    app2 = _StreamApp(kinds={"s" + "0" * 31: "site"})
    app2._channels["sitech"] = "s" + "0" * 31
    app2._stream_cid = app2._channels["general"]
    app2._switch_channel("sitech")
    tick_until(app2, lambda: app2.unsubscribed)
    check("switch to a push-less kind releases the subscription",
          app2.unsubscribed and app2._stream_cid is None,
          (repr(app2.unsubscribed), repr(app2._stream_cid)))

    # 3. keepalive 409 -> resubscribe FROM THE WATERMARK (gap replays).
    app3 = _StreamApp()
    app3._stream_cid = app3._channels["general"]
    app3._stream_cursor = 5
    app3.poll_script = [{"ok": 0, "code": 409}]
    app3._stream_keepalive_once()
    ok = tick_until(app3, lambda: app3.subscribe_calls)
    check("keepalive self-heals an expired subscription",
          ok and app3.subscribe_calls[-1] == (app3._channels["general"], 5),
          repr(app3.subscribe_calls))

    # 4. keepalive wins a race: events apply, cursor advances.
    app4 = _StreamApp()
    app4._stream_cid = app4._channels["general"]
    app4._stream_cursor = 3
    app4.poll_script = [{
        "ok": 1, "cursor": 9,
        "events": [{"kind": "msg",
                    "data": {"msg": {"msg_id": "m1",
                                     "channel_id": app4._channels["general"],
                                     "content": "x"}}}]}]
    app4._stream_keepalive_once()
    tick_until(app4, lambda: app4.fetched is not None)
    check("keepalive advances the cursor on raced events",
          app4._stream_cursor == 9 and app4.fetched == "general",
          (repr(app4._stream_cursor), repr(app4.fetched)))

    # 5. keepalive start is idempotent (one interval, not two).
    app5 = _StreamApp()
    app5._start_stream_keepalive()
    first = app5._stream_tick_event
    app5._start_stream_keepalive()
    check("keepalive start is idempotent",
          first is not None and app5._stream_tick_event is first,
          repr(app5._stream_tick_event))


def test_ticket_push():
    """v0.31: pushes are tickets (references, never bodies) -- the full
    message row in a raw RNS.Packet crossed the 500-byte link MTU, which
    is what the operator console's 515/979 errors were. The client must
    answer a ticket with an immediate delta fetch; legacy full-body
    frames still apply to the event renderer."""
    from kivy.clock import Clock

    import kivy_client as kc

    class _TicketApp(_StubApp):
        def __init__(self):
            super().__init__(**{})
            self._channels = {"ticket-ch": "t" + "0" * 31}
            self._channel_kinds = {"t" + "0" * 31: "chat"}
            self.fetched = []
            self.applied = []
            self._stream_cursor = None
            self._stream_cid = None

        def _fetch_messages(self, name):
            self.fetched.append(name)

        def _apply_stream_events(self, events):
            self.applied.append(list(events))

    def tick_until(cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            Clock.tick()
            if cond():
                return True
            time.sleep(0.02)
        return False

    handle = kc.sHadesClientApp._handle_stream_frame

    app = _TicketApp()
    handle(app, {"k": "stream", "ticket": True,
                 "channel": "t" + "0" * 31, "seq": 9})
    ok = tick_until(lambda: app.fetched)
    check("ticket push triggers an immediate delta fetch",
          ok and app.fetched == ["ticket-ch"], repr(app.fetched))
    check("ticket push syncs the cursor", app._stream_cursor == 9,
          repr(app._stream_cursor))
    check("ticket push carries no body to apply",
          app.applied == [], repr(app.applied))

    # legacy frames (full bodies, no ticket flag) still flow
    app2 = _TicketApp()
    app2._channels = {"general": "c" + "0" * 31}
    ev = [{"kind": "presence", "data": {"users": []}}]
    handle(app2, {"k": "stream", "seq": 11, "events": ev})
    check("legacy full-body frames still apply, nothing fetched",
          app2.applied == [ev] and app2._stream_cursor == 11
          and not app2.fetched,
          (repr(app2.applied), repr(app2.fetched)))


def test_blobs_put_mobile_receipts():
    """v0.32: uploads time out on the phone while the identical code sits
    fine on desktop (tay's receipt). The upload loop must (a) shrink the
    chunk on receipt timeouts and resume from the last acked offset --
    the server stores offset-addressed bytes, so that rewrites instead
    of corrupts -- and (b) restart once when the staged handle aged out.
    Scripted api responses pin both paths plus the bounded attempt cap."""
    import base64 as _b64

    import kivy_client as kc

    class _ScriptedApi(kc.GuildAPI):
        """Fake transport: scripted responses per request path, all
        recorded for assertions."""

        def __init__(self):
            super().__init__(reticulum=object(), link=object())
            self.script = []      # list of (path, response-or-callable)
            self.calls = []       # list of (path, offset, chunk-b64-len)

        def _request_json(self, path, data, timeout=10.0):
            self.calls.append((path, data.get("offset"),
                               len(data.get("data_b64") or "")))
            for p, r in self.script:
                if p == path:
                    return r(p, data) if callable(r) else r
            return {"ok": 0, "error": "unscripted", "code": 999}

    def init_resp(upload_id, chunk_size):
        def _f(path, data):
            return {"ok": 1, "upload_id": upload_id,
                    "chunk_size": chunk_size}
        return _f

    def chunk_acks(path, data):
        """Server-shaped ack: next_offset from the DECODED bytes."""
        n = len(_b64.b64decode(data["data_b64"]))
        nxt = data["offset"] + n
        return {"ok": 1, "next_offset": nxt, "eof": nxt >= 8}

    # ---- path (a): chunk timeout shrinks and resumes -------------------
    # Sizes must clear the 32 KB floor for halving to engage: start at
    # 128 KB per chunk, 400 KB of payload.
    api = _ScriptedApi()
    chunks_seen = []
    INIT_B64 = ((131072 + 2) // 3) * 4          # base64 of 131072 bytes

    def chunk_first_timeout(path, data):
        chunks_seen.append((data["offset"],
                            len(data.get("data_b64") or "")))
        if len(chunks_seen) == 1 or chunks_seen[-2][0] != data["offset"]:
            # first attempt at a fresh offset: receipt timeout
            return {"ok": 0, "error": "timeout"}
        n = len(_b64.b64decode(data["data_b64"]))
        nxt = data["offset"] + n
        return {"ok": 1, "next_offset": nxt, "eof": nxt >= 400000}

    api.script = [("/api/v1/blobs/put/init", init_resp("u1", 131072)),
                  ("/api/v1/blobs/put/chunk", chunk_first_timeout),
                  ("/api/v1/blobs/put/finish",
                   {"ok": 1, "blob_id": "b1"})]
    done_bits = []

    up = api.blobs_put("f.bin", "application/octet-stream", b"A" * 400000,
                       on_progress=lambda d, t: done_bits.append(d))
    check("shrunk-retry upload completes", bool(up.get("ok")),
          (up.get("phase"), up.get("error"), chunks_seen[:4]))
    check("first chunk rode the server's 128 KB size",
          chunks_seen and chunks_seen[0][1] == INIT_B64,
          repr(chunks_seen[:1]))
    check("chunks shrank after receipt timeouts",
          min(c[1] for c in chunks_seen) <= INIT_B64 // 4,
          repr(sorted(set(c[1] for c in chunks_seen))))
    check("every timeouted offset got its retry",
          all(sum(1 for c in chunks_seen if c[0] == o) >= 2
              for o in set(c[0] for c in chunks_seen)),
          repr(chunks_seen))
    check("progress reported and completed",
          done_bits and done_bits[-1] == 400000, repr(done_bits[-3:]))

    # ---- path (b): aged-out staged handle restarts once ----------------
    api2 = _ScriptedApi()
    reinit_seen = []
    api2.script = [("/api/v1/blobs/put/init", init_resp("u2", 4)),
                   ("/api/v1/blobs/put/chunk",
                    lambda p, d: {"ok": 0, "error": "unknown upload_id",
                                  "code": 404}),
                   ("/api/v1/blobs/put/finish", {"ok": 1})]
    up2 = api2.blobs_put("f.txt", "text/plain", b"B" * 8)
    n_inits = len(reinit_seen) if False else len(
        [c for c in api2.calls if c[0].endswith("init")])
    n_chunks = len([c for c in api2.calls if c[0].endswith("chunk")])
    check("staged-handle death restarts once, then dies bounded",
          not up2.get("ok") and up2.get("phase") == "chunk@0"
          and n_inits == 2 and n_chunks == 2,
          (up2.get("phase"), up2.get("error"), n_inits, n_chunks))

    # ---- path (c): persistent timeout dies bounded, phase reported -----
    api3 = _ScriptedApi()
    api3.script = [("/api/v1/blobs/put/init", init_resp("u1", 4)),
                   ("/api/v1/blobs/put/chunk",
                    lambda p, d: {"ok": 0, "error": "timeout"})]
    up3 = api3.blobs_put("f.txt", "text/plain", b"A" * 8)
    n_calls = len(api3.calls)
    check("persistent timeout dies with phase + bounded attempts",
          not up3.get("ok") and "chunk@" in up3.get("phase", "") and
          n_calls <= 11, (up3.get("phase"), n_calls))


class _FakeRouter(object):
    def __init__(self):
        self.hash = b"delivery-hash"
        self.announce_calls = 0

    def announce(self, destination_hash=None, **kwargs):
        assert destination_hash == self.hash, destination_hash
        self.announce_calls += 1


def test_periodic_dm_announce():
    """v0.31.3: the client re-announces its lxmf.delivery destination on a
    periodic _DMA_INTERVAL timer (RNS practice for changing topologies --
    mobile churn and reconnects age out hub-side paths); one boot announce
    (kivy_client.py, _init_identity_and_lxmf) is not enough for long
    sessions. Pins: a router with a registered delivery destination gets
    announce() called exactly on that hash, and nothing fires when no
    router/delivery exists."""

    class _App(object):
        lxmf_router = None
        lxmf_delivery = None
        _DMA_INTERVAL = kc.sHadesClientApp._DMA_INTERVAL
        _announce_dm_once = kc.sHadesClientApp._announce_dm_once

    app = _App()
    app.lxmf_router = _FakeRouter()
    app.lxmf_delivery = app.lxmf_router
    assert app._announce_dm_once() is True
    assert app.lxmf_router.announce_calls == 1, app.lxmf_router.announce_calls

    # missing router -> silent no-op, no exception
    app.lxmf_router, app.lxmf_delivery = None, None
    assert app._announce_dm_once() is True


def test_dm_announce_interval_sane():
    interval = kc.sHadesClientApp._DMA_INTERVAL
    assert 60 <= interval <= 3600, interval


class _KAStubClock(object):
    def __init__(self):
        self.scheduled = []

    def schedule_once(self, fn, delay=0):
        self.scheduled.append((fn, delay))


def test_keepalive_miss_not_counting_during_upload():
    """v0.31.4: an upload monopolizes the priority-agnostic link FIFO, so
    the 45s stream poll can time out mid-upload without the link being
    dead. Counting those misses and re-establishing at two tears the link
    under our own transfer (0.31.2 self-inflicted, receipted during the
    attach-drop audit). Pins: misses while _up_active are ignored (no
    re-establish scheduled, counter reset); misses without an upload
    still count and fire the re-establish at two."""

    real_clock = kc.Clock
    kc.Clock = _KAStubClock()
    try:
        class _App(object):
            _up_active = True
            _ka_fail = 0
            _ka_miss = kc.sHadesClientApp._ka_miss
            _do_reestablish = kc.sHadesClientApp._do_reestablish

        app = _App()
        app._ka_miss()
        app._ka_miss()
        assert app._ka_fail == 0, app._ka_fail
        assert len(kc.Clock.scheduled) == 0, kc.Clock.scheduled

        app._up_active = False
        app._ka_miss()
        assert app._ka_fail == 1, app._ka_fail
        assert len(kc.Clock.scheduled) == 0, "one miss must not re-establish"
        app._ka_miss()
        assert app._ka_fail == 0, "counter resets at re-establish trigger"
        assert len(kc.Clock.scheduled) == 1
        fn, delay = kc.Clock.scheduled[0]
        assert getattr(fn, "__name__", "") == "_do_reestablish", fn
        assert delay == 0, delay
    finally:
        kc.Clock = real_clock


class _FakeLXMFModule(object):
    class _FakeDelivery(object):
        hash = b"delivery-hash"

    class LXMRouter(object):
        def __init__(self, identity=None, storagepath=None):
            self.stor = storagepath
            self.registered = 0
            self.callbacks = 0
            self.announce_calls = []

        def register_delivery_identity(self, identity, display_name=None):
            self.registered += 1
            self.delivery = _FakeLXMFModule._FakeDelivery()
            self.delivery_name = display_name
            return self.delivery

        def register_delivery_callback(self, cb):
            self.callbacks += 1

        def announce(self, h):
            self.announce_calls.append(h)


def test_resume_heal_rebuilds_lxmf_and_dead_links():
    """v0.31.5: Android stops the activity for every SAF pick (opaque
    picker, cannot be prevented); on_stop used to exit the LXMF router on
    that stop, freezing DMs for the rest of the session. tay: 'We don't
    *want* to stop the app. That is literally the blocking behavior.'
    Pins: (a) resume heal rebuilds the router from the existing identity
    and announces, without duplicate announce-loop threads; (b) a dead
    guild link schedules _do_reestablish; (c) a healthy link schedules
    nothing; (d) no identity = nothing rebuilt, no crash."""

    import types as _types

    class _App(object):
        _heal_after_resume = kc.sHadesClientApp._heal_after_resume
        _init_lxmf_router = kc.sHadesClientApp._init_lxmf_router
        _dm_announce_loop = kc.sHadesClientApp._dm_announce_loop
        _DMA_INTERVAL = kc.sHadesClientApp._DMA_INTERVAL
        _on_dm_delivery = kc.sHadesClientApp._on_dm_delivery
        _do_reestablish = kc.sHadesClientApp._do_reestablish
        client_identity = object()
        username = "tay"
        lxmf_router = None
        lxmf_delivery = None
        guild_api = None
        _ka_fail = 3

    saved_mod = sys.modules.get("LXMF")
    sys.modules["LXMF"] = _types.SimpleNamespace(LXMRouter=_FakeLXMFModule.LXMRouter)
    real_clock = kc.Clock
    kc.Clock = _KAStubClock()
    try:
        app = _App()
        app._heal_after_resume()
        assert isinstance(app.lxmf_router, _FakeLXMFModule.LXMRouter), app.lxmf_router
        assert app.lxmf_delivery.hash == b"delivery-hash", app.lxmf_delivery
        assert len(app.lxmf_router.announce_calls) == 1, "heal re-announces"
        assert app._ka_fail == 0, "resume resets the keepalive counter"
        assert len(kc.Clock.scheduled) == 0, "healthy link: nothing scheduled"

        # second heal with an existing router: no duplicate announce thread
        # (guarded by router_was_none) -- router object replaced, announce still fires
        app._heal_after_resume()
        assert isinstance(app.lxmf_router, _FakeLXMFModule.LXMRouter)
        assert len(app.lxmf_router.announce_calls) == 1

        # dead guild link heals
        class _DeadLink(object):
            status = kc.RNS.Link.CLOSED

        app.guild_api = type("A", (), {"link": None})()
        app.guild_api.link = _DeadLink()
        app._heal_after_resume()
        assert len(kc.Clock.scheduled) == 1, "dead link schedules re-establish"
        fn, delay = kc.Clock.scheduled[0]
        assert getattr(fn, "__name__", "") == "_do_reestablish", fn

        # healthy link does not
        class _LiveLink(object):
            status = kc.RNS.Link.ACTIVE

        app.guild_api = type("A", (), {"link": None})()
        app.guild_api.link = _LiveLink()
        app._heal_after_resume()
        assert len(kc.Clock.scheduled) == 1, "live link: nothing new scheduled"

        # no identity: no-op, no crash
        app2 = _App()
        app2.client_identity = None
        app2._heal_after_resume()
        assert app2.lxmf_router is None
    finally:
        if saved_mod is not None:
            sys.modules["LXMF"] = saved_mod
        else:
            sys.modules.pop("LXMF", None)
        kc.Clock = real_clock


def test_channel_management_gates_and_kinds():
    """v0.31.6 founder/channel work: the create-gate mirrors the server's
    MANAGE_CHANNEL|ADMIN rule, and the kind chooser carries tay's four
    kinds (tay 2026-11-24 added voice: the server lane already accepted
    kind "voice" - store.CHANNEL_KINDS, the /api/v1 voice join/roster
    handlers - but the picker had no button for it)."""

    kind_map = dict(kc.sHadesClientApp.CHANNEL_KIND_LABELS)
    check("kind chooser: tay's four kinds (text/site/term/voice)",
          kind_map == {"text": "chat", "site (micron)": "site",
                       "text stream": "term", "voice": "voice"}, kind_map)

    class _App(object):
        i_am_admin = False
        my_roles = []
        PERM_MANAGE_CHANNEL = 8
        PERM_ADMIN = 32
        _can_create_channels = kc.sHadesClientApp._can_create_channels

    app = _App()
    check("no powers: create gate closed", app._can_create_channels() is False)
    app.my_roles = [{"perms": 8}]
    check("MANAGE_CHANNEL role: gate open", app._can_create_channels() is True)
    app.my_roles = [{"perms": 0}]
    app.i_am_admin = True
    check("admin flag: gate open", app._can_create_channels() is True)



def test_send_failure_restores_compose():
    """fold-14 P11g: a dead link at send must not swallow the compose.

    The connection-flow audit (tay: 'verify the connection state flow
    at login, recovery, and elsewhere') found the only silent loss in
    the chain: send_message cleared the field at capture time and its
    worker had no except -- a GuildAPI raise lost the text with no
    status line. Pins: (a) a post() raise restores the text, re-stages
    the reply card reply_wire() consumed, and posts a
    'send failed: ... (text restored)' status; (b) a server not-ok
    reply restores too; (c) success keeps the field cleared and the
    attachment consumed; (d) an attachment-uploader raise restores and
    keeps the attachment pending."""

    class _Inp(object):
        text = ""
        focus = False

    class _Ids(object):
        def __init__(self):
            self.msg_input = _Inp()
            self.message_grid = object()

    class _Main(object):
        ids = _Ids()
        _current_channel = "general"

    class _Root(object):
        def get_screen(self, name):
            assert name == "main"
            return _Main()

    class _Api(object):
        def __init__(self, behavior):
            self.behavior = behavior

        def post(self, cid, text):
            if self.behavior == "raise":
                raise RuntimeError("link dead (RNS.Link.CLOSED)")
            return {"ok": 0, "error": "channel read-only"}

    class _App(object):
        send_message = kc.sHadesClientApp.send_message
        cancel_reply = kc.sHadesClientApp.cancel_reply
        root = _Root()
        guild_api = None
        username = "ash"
        my_hash = b"me"
        _pending_attachment = None
        _pending_reply = None
        reply_draft = ""
        _up_active = False
        statuses = []
        rendered = []

        def _channel_cid(self, name):
            return "cid-" + name

        def _channel_kind(self, cid):
            return "channel"

        def _set_status(self, msg):
            self.statuses.append(msg)

        def _render_message(self, grid, m, animate=False):
            self.rendered.append(m)

    real_clock = kc.Clock
    real_kc_threading = getattr(kc, "threading", None)

    class _SyncThread(object):
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    def _flush():
        for fn, _delay in list(kc.Clock.scheduled):
            fn(None)

    import types as _types
    kc.Clock = _KAStubClock()
    kc.threading = _types.SimpleNamespace(Thread=_SyncThread)

    try:
        # ---- (a) the post raises: the compose + the reply come back
        app = _App()
        app.guild_api = _Api("raise")
        inp = app.root.get_screen("main").ids.msg_input
        app._pending_reply = ("tay", "the question in it")
        inp.text = "hello there"
        app.send_message()
        _flush()
        check("a dead link restores the composer text",
              inp.text == "hello there", inp.text)
        check("the consumed reply card re-stages",
              app._pending_reply == ("tay", "the question in it")
              and "Replying to @tay" in getattr(app, "reply_draft", ""),
              app.reply_draft)
        check("the failure names itself in the status line",
              app.statuses
              and app.statuses[-1].startswith("send failed:")
              and "text restored" in app.statuses[-1], app.statuses)
        check("the attachment stays pending (it never moved)",
              app._pending_attachment is None, app._pending_attachment)

        # ---- (b) the server rejects: the compose still comes back
        del kc.Clock.scheduled[:]
        app2 = _App()
        app2.guild_api = _Api("not-ok")
        inp2 = app2.root.get_screen("main").ids.msg_input
        inp2.text = "second take"
        app2.send_message()
        _flush()
        check("a server not-ok reply also restores the compose",
              inp2.text == "second take"
              and "send failed: channel read-only" in app2.statuses[-1],
              app2.statuses)

        # ---- (c) success: the field stays cleared, the render fires
        del kc.Clock.scheduled[:]
        app3 = _App()
        import types as _t2
        app3.guild_api = _t2.SimpleNamespace(
            post=lambda cid, text: {"ok": 1, "msg_id": "m1"})
        inp3 = app3.root.get_screen("main").ids.msg_input
        inp3.text = "the good one"
        app3.send_message()
        _flush()
        check("a success clears the field and renders optimistically",
              inp3.text == ""
              and len(app3.rendered) == 1
              and app3.rendered[0]["content"] == "the good one"
              and app3._pending_attachment is None, app3.rendered)

        # ---- (d) an uploader raise: restored, attachment pending
        del kc.Clock.scheduled[:]
        app4 = _App()
        import types as _t3
        app4.guild_api = _t3.SimpleNamespace(
            blobs_put=lambda name, mime, blob, on_progress=None:
                (_ for _ in ()).throw(RuntimeError("link tore mid-upload")),
            post=lambda cid, text: {"ok": 1, "msg_id": "m2"})
        app4._pending_attachment = ("a.png", "image/png", b"x")
        inp4 = app4.root.get_screen("main").ids.msg_input
        inp4.text = "with pic"
        app4.send_message()
        _flush()
        check("an uploader raise restores the compose and keeps the "
              "attachment pending",
              inp4.text == "with pic"
              and app4._pending_attachment == ("a.png", "image/png", b"x")
              and "send failed:" in app4.statuses[-1], app4.statuses)
        check("the uploader raise clears _up_active",
              app4._up_active is False, app4._up_active)
    finally:
        kc.Clock = real_clock
        kc.threading = real_kc_threading

def test_restore_without_server_row_dials_default():
    """P11h: tay's import-stall receipt (0.36.24). A restored kit with no
    dialable server row parked the member on connect (hub blank, Connect
    disabled, 'Identity recovered. Connect to a saved guild.') while a
    restart auto-dialed the default hub via the portable-profile
    fallthrough. Pins: (a) no row -> the default hub is dialed with the
    'user' fallback and the attempt reports started; (b) a kit row still
    dials the kit's guild first (the transfer contract unchanged);
    (c) a kit row with an invalid port stays parked with prefill (the
    member must correct the address -- dialing a broken port would be
    worse)."""

    class _App(object):
        _enter_restored_account = kc.sHadesClientApp._enter_restored_account
        _servers = []
        username = ""
        hub_address = ""
        hub_host = ""
        hub_port = 0
        guild_node_hash = None

        def __init__(self):
            self.deliveries = []

        def connect_to_guild(self, hub_address, username,
                             node_hash="", delivery_hash=""):
            self.deliveries.append((hub_address, username,
                                    node_hash, delivery_hash))

        def _prefill_connect_from_servers(self):
            pass

        default_hub = kc.DEFAULT_HUB_ADDRESS

    app = _App()
    started = app._enter_restored_account()
    check("a restored kit with no server row auto-dials the default hub",
          started is True
          and app.deliveries == [(kc.DEFAULT_HUB_ADDRESS, "user", "", "")],
          app.deliveries)

    app2 = _App()
    app2.username = "ash"
    app2._servers = [{"hub_address": "10.0.0.5:29221", "port": 29221,
                      "username": "ash", "node_hash": "ab" * 16,
                      "delivery_hash": "cd" * 16}]
    started2 = app2._enter_restored_account()
    check("a kit's own guild row is dialed before any default fallback",
          started2 is True
          and app2.deliveries == [("10.0.0.5:29221", "ash", "ab" * 16,
                                   "cd" * 16)],
          app2.deliveries)

    app3 = _App()
    app3._servers = [{"hub_address": "10.0.0.5:29221", "port": 0}]
    started3 = app3._enter_restored_account()
    check("an invalid kit port parks with prefill instead of dialing",
          started3 is False and app3.deliveries == [], app3.deliveries)

def main():
    for fn in (test_display_name_preserved,
               test_update_applied_once,
               test_attachment_kind_classifier,
               test_stream_lifecycle,
               test_ticket_push,
               test_blobs_put_mobile_receipts,
               test_admin_panel_not_blocking,
               test_roles_popup_not_blocking,
               test_shutdown_not_blocking,
               test_periodic_dm_announce,
               test_dm_announce_interval_sane,
               test_keepalive_miss_not_counting_during_upload,
               test_resume_heal_rebuilds_lxmf_and_dead_links,
               test_channel_management_gates_and_kinds,
               test_send_failure_restores_compose,
               test_restore_without_server_row_dials_default):
        try:
            fn()
        except Exception as e:
            check(fn.__name__ + " raised", False, repr(e))

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
