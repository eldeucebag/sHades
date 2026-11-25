#!/usr/bin/env python3
"""v0.31.2 link self-heal regression: a dead or risen-from-the-dead link
must never leave the client mute until restart (tay's exact symptom:
"cannot send chat messages until client is restarted").

Pins:

  1. GuildAPI._request_json on a non-ACTIVE link returns
     {"ok": 0, "error": "link dead"} and signals on_dead exactly once.
     Callers surface that as a visible failure instead of a worker
     thread dying silently in a receipt wait.

  2. A link.request() raise (RNS refuses a dead link) gives the same
     result through the on_dead path.

  3. A PLAIN object link (no .status attribute -- the legacy shape every
     pre-self-heal test stub uses) performs NO dead-check and reaches
     the transport as before. The self-heal must not rewire tests or
     stubs that never carried liveness state.
"""

import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RNS  # noqa: E402

import kivy_client as kc  # noqa: E402


class _DeadLink(object):
    """RNS-shaped link stuck CLOSED: status present, sends refused."""

    def __init__(self):
        self.status = RNS.Link.CLOSED
        self.asks = 0

    def request(self, *a, **k):
        self.asks += 1
        raise RuntimeError("refusing: link CLOSED")


def test_link_dead_reported_and_signalled():
    seen = []

    api = kc.GuildAPI(object(), _DeadLink(),
                      on_dead=lambda st: seen.append(("status", st)))

    res = api._request_json("/api/v1/post", {"text": "hello"})
    assert res.get("ok") == 0, res
    assert res.get("error") == "link dead", res
    assert len(seen) == 1, seen
    assert seen[0][0] == "status", seen

    # the dead check fires BEFORE the transport sees the call
    assert api.link.asks == 0, api.link.asks

    res2 = api._request("/api/v1/post", {"text": "hello"})
    assert res2.get("ok") == 0 and res2.get("error") == "link dead", res2
    assert len(seen) == 2, seen


def test_link_raise_reported_and_signalled():
    class _RaiseLink(object):
        status = RNS.Link.ACTIVE

        def request(self, *a, **k):
            raise RuntimeError("rns refused the call")

    seen = []
    api = kc.GuildAPI(object(), _RaiseLink(),
                      on_dead=lambda st: seen.append(st))
    res = api._request_json("/api/v1/post", {"text": "hello"})
    assert res.get("ok") == 0, res
    assert res.get("error") == "link dead", res
    assert seen == ["raise"], seen


def test_plain_object_link_untouched():
    """No .status attribute -> no dead-check -> request reaches transport.

    Every pre-self-heal stub (GuildAPI(reticulum=..., link=object()))
    depends on this: object() has no status attribute, and getattr must
    treat that as "liveness unknown", not "dead".
    """

    class _PlainLink(object):
        def request(self, *a, **k):
            return {"sent": True}

    seen = []
    api = kc.GuildAPI(object(), _PlainLink(),
                      on_dead=lambda st: seen.append(st))
    res = api._request_json("/api/v1/post", {"text": "hello"})
    # _request_json proceeds to the receipt-wait path; a dict receipt has
    # no .response, so the result is a timeout -- but it must NOT be
    # "link dead", and on_dead must not have fired.
    assert res.get("error") != "link dead", res
    assert seen == [], seen


def test_heal_never_nulls_healthy_router():
    """0.31.8 (boot.log receipt): _init_lxmf_router used to rebuild a HEALTHY
    router whenever the resume heal met it -- every SAF pick -- then crash
    its own rebuild with KeyError('Attempt to register an already
    registered destination.') and null the router in the except: the
    healer itself disabled DMs ("LXMF router init failed (DMs disabled)")
    boot after boot. A healthy router must be left alone.
    """

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    app.client_identity = object()
    router = object()
    delivery = object()
    app.lxmf_router = router
    app.lxmf_delivery = delivery

    app._init_lxmf_router()

    assert app.lxmf_router is router, "heal rebuilt a healthy router"
    assert app.lxmf_delivery is delivery, "heal rebuilt a healthy delivery"


def test_heal_rebuild_only_when_router_gone():
    """router None -> the rebuild path is reached (recovery still exists
    for the genuinely-dead case; the old boot-time behavior is intact).
    The rebuild attempt is observed by substituting the LXMF import.
    """

    import types

    built = {"n": 0}

    class _FakeRouter(object):
        def __init__(self, identity=None, storagepath=None):
            built["n"] += 1

        def register_delivery_identity(self, identity, display_name=None):
            return types.SimpleNamespace(hash=b"fake-delivery-hash")

        def register_delivery_callback(self, cb):
            pass

        def announce(self, *_a, **_k):
            pass

    fake = types.ModuleType("LXMF")
    fake.LXMRouter = _FakeRouter
    saved = sys.modules.get("LXMF")
    saved_appdir = getattr(kc, "_app_data_dir", None)
    sys.modules["LXMF"] = fake
    kc._app_data_dir = lambda: "/tmp/sHades-lxmf-test"
    built["n"] = 0
    try:
        app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
        app.client_identity = object()
        app.lxmf_router = None
        app.lxmf_delivery = None
        app.username = "tester"
        app._init_lxmf_router()
        assert built["n"] == 1, built
        assert app.lxmf_router is not None, "rebuild path dead in 0.31.8"
    finally:
        if saved is not None:
            sys.modules["LXMF"] = saved
        else:
            sys.modules.pop("LXMF", None)
        if saved_appdir is not None:
            kc._app_data_dir = saved_appdir


def test_blob_upload_retries_timeout_then_posts():
    """0.31.8 (boot.log receipt): every attach died at the first blob_put
    that straddled the ~5 s SAF-picker socket gap ([Errno 103] -> RNS
    reconnect), then fell through to the legacy inline post, which also
    failed ("failed with status: 07"). Chunked uploads are offset-addressed
    and idempotent, so the flow retries; a success posts by reference and
    the legacy inline post is never reached.
    """

    class _Fakes(object):
        pass

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    calls = {"blob": [], "post_v1": [], "legacy": [], "sleep": [],
             "up_active": []}

    main_screen = _Fakes()
    main_screen._current_channel = "general"
    main_screen.ids = _Fakes()
    main_screen.ids.message_grid = object()
    main_screen.ids.msg_input = _Fakes()
    main_screen.ids.msg_input.helper_text = None

    class _Root(object):
        def get_screen(self, *_a, **_k):
            return main_screen

    app.root = _Root()
    app._channel_cid = lambda cid: cid
    app._set_status = lambda msg: None
    app.my_hash = "aa" * 16
    app.username = "tester"
    app._add_to_history = lambda m: None
    app._save_history = lambda: None
    app._render_message = lambda *a, **k: None
    app._save_attachment_blob = lambda blob, name: "/tmp/fake"

    class _API(object):
        link = object()  # plain stub: liveness unknown -> not dead

        def blobs_put(self, name, mime, blob, on_progress=None):
            calls["blob"].append(name)
            calls["up_active"].append(app._up_active)
            if on_progress is not None:
                on_progress(100, 200)  # half done -> 50%
            if len(calls["blob"]) < 3:
                return {"ok": 0, "error": "timeout"}
            return {"ok": 1, "blob_id": "b1"}

        def post_v1(self, cid, text, attachments=None):
            calls["post_v1"].append(attachments)
            return {"ok": 1, "msg_id": "m1",
                    "attachment": {"path": "/file/attachments/x.bin"}}

        def post(self, cid, text, attachments=None):
            calls["legacy"].append(True)
            return {"ok": 1}

    app.guild_api = _API()

    real_threading = kc.threading
    real_time = kc.time
    real_clock = kc.Clock

    class _NowClock(object):
        @staticmethod
        def schedule_once(cb, dt=0):
            cb(dt)

    import types as _t

    shim_threading = _t.ModuleType("threading")

    class _SyncThread(object):
        def __init__(self, target=None, daemon=False, *a, **k):
            self._target = target

        def start(self):
            self._target()

    shim_threading.Thread = _SyncThread
    shim_time = _t.ModuleType("time")

    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: calls["sleep"].append(s)
    kc.threading = shim_threading
    kc.time = shim_time
    kc.Clock = _NowClock
    try:
        app._send_with_attachment("hi", "f.bin", "application/octet-stream",
                                  b"data", kc.ATT_SERVER)
    finally:
        kc.threading = real_threading
        kc.time = real_time
        kc.Clock = real_clock

    assert len(calls["blob"]) == 3, calls
    assert all(calls["up_active"]), "0.31.4 upload gate dropped on retry"
    assert len(calls["post_v1"]) == 1, calls
    assert calls["legacy"] == [], "legacy inline post ran on a server-stored hop"
    assert calls["sleep"], "no backoff between retries"
    for s in calls["sleep"]:
        assert 0 < s <= 8.0, calls["sleep"]

    # 0.31.10: the composer helper text must have shown the upload percent
    # during the flow and be cleared when the message lands.
    inp = main_screen.ids.msg_input

    def seen_input():
        return getattr(inp, "helper_text", None)

    assert seen_input() == "", \
        "composer not cleared after delivery: %r" % seen_input()


def test_blob_retry_never_inlines_big_blob_on_timeout():
    """Retries exhausted with the link still unresolvable -> the worker must
    NOT push the full blob inline (the receipted 7 MB failure mode); it
    surfaces a status instead.
    """

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    calls = {"blob": 0, "post_v1": 0, "legacy": 0}

    class _Fakes(object):
        pass

    main_screen = _Fakes()
    main_screen._current_channel = "general"
    main_screen.ids = _Fakes()

    class _Root(object):
        def get_screen(self, *_a, **_k):
            return main_screen

    app.root = _Root()
    app._channel_cid = lambda cid: cid
    app._set_status = lambda msg: calls.setdefault("status", []).append(msg)
    app.my_hash = "aa" * 16
    app.username = "tester"

    class _DeadLink(object):
        status = RNS.Link.CLOSED

    class _API(object):
        link = _DeadLink()

        def blobs_put(self, name, mime, blob, on_progress=None):
            calls["blob"] += 1
            return {"ok": 0, "error": "timeout"}

        def post_v1(self, *a, **k):
            calls["post_v1"] += 1
            return {"ok": 0}

        def post(self, *a, **k):
            calls["legacy"] += 1
            return {"ok": 1}

    app.guild_api = _API()

    real_threading = kc.threading
    real_time = kc.time
    real_clock = kc.Clock

    class _NowClock(object):
        @staticmethod
        def schedule_once(cb, dt=0):
            cb(dt)

    shim = types.ModuleType("shim-threading")

    class _SyncThread(object):
        def __init__(self, target=None, daemon=False):
            self._target = target

        def start(self):
            self._target()

    shim.Thread = _SyncThread
    kc.threading = shim
    saves = []
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: 0.0
    shim_time.sleep = saves.append
    kc.time = shim_time
    kc.Clock = _NowClock
    try:
        app._send_with_attachment("hi", "f.bin", "application/octet-stream",
                                  b"data", kc.ATT_SERVER)
    finally:
        kc.threading = real_threading
        kc.time = real_time
        kc.Clock = real_clock

    assert calls["blob"] == 1, "retried against a dead link"
    assert calls["legacy"] == 0, "inline blob pushed at a dead link"
    assert calls["post_v1"] == 0, calls
    assert any("upload failed" in m and "retry the attach" in m
               for m in calls.get("status", [])), calls


def test_blob_linkdead_never_falls_through():
    """0.31.9 (boot_2 receipt 13:23:20): tay's phone -- attempt 1 timed out
    in the socket gap, the keepalive reaper tore the guild link during the
    sleep ("guild link dead (3)"), attempt 2 saw "link dead", and the
    timeout-only fallthrough guard let the LEGACY inline post run anyway
    ("post response ok=0" on the full blob). Any blobs_put failure must
    end the flow: no retry after link dead, no legacy inline post, a
    status telling the user to retry the attach.

    Also pins the gate fix: _up_active must STILL be held across the
    inter-attempt sleep, so the reaper cannot tear the link mid-retry.
    """

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    calls = {"blob": 0, "post_v1": 0, "legacy": 0, "up_at_rest": []}

    class _Fakes(object):
        pass

    main_screen = _Fakes()
    main_screen._current_channel = "general"
    main_screen.ids = _Fakes()

    class _Root(object):
        def get_screen(self, *_a, **_k):
            return main_screen

    app.root = _Root()
    app._channel_cid = lambda cid: cid
    app._set_status = lambda msg: calls.setdefault("status", []).append(msg)
    app.my_hash = "aa" * 16
    app.username = "tester"
    app._add_to_history = lambda m: None
    app._save_history = lambda: None
    app._render_message = lambda *a, **k: None
    app._save_attachment_blob = lambda blob, name: "/tmp/fake"

    class _API(object):
        link = object()  # plain stub: liveness unknown while retry rides

        def blobs_put(self, name, mime, blob, on_progress=None):
            calls["blob"] += 1
            if calls["blob"] < 2:
                return {"ok": 0, "error": "timeout"}
            return {"ok": 0, "error": "link dead"}

        def post_v1(self, *a, **k):
            calls["post_v1"] += 1
            return {"ok": 0}

        def post(self, *a, **k):
            calls["legacy"] += 1
            return {"ok": 1}

    app.guild_api = _API()

    real_threading = kc.threading
    real_time = kc.time
    real_clock = kc.Clock

    class _NowClock(object):
        @staticmethod
        def schedule_once(cb, dt=0):
            cb(dt)

    shim = types.ModuleType("shim-threading")

    class _SyncThread(object):
        def __init__(self, target=None, daemon=False):
            self._target = target

        def start(self):
            self._target()

    shim.Thread = _SyncThread
    kc.threading = shim

    def _sleep(_s):
        calls["up_at_rest"].append(app._up_active)

    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: 0.0
    shim_time.sleep = _sleep
    kc.time = shim_time
    kc.Clock = _NowClock
    try:
        app._send_with_attachment("hi", "f.bin", "application/octet-stream",
                                  b"data", kc.ATT_SERVER)
    finally:
        kc.threading = real_threading
        kc.time = real_time
        kc.Clock = real_clock

    assert calls["blob"] == 2, calls
    assert calls["up_at_rest"] == [True], \
        "gate released between attempts; reaper tore the link mid-retry"
    assert calls["legacy"] == 0, \
        '"link dead" fell through to the legacy inline post'
    assert calls["post_v1"] == 0, calls
    assert any("link dead" in m for m in calls.get("status", [])), calls
    assert not app._up_active, "gate stuck held after the flow finished"


def test_wait_path_mtu_bounded_and_uses_live_path():
    """0.31.8 (boot.log receipt): re-establishes ran with an invalidated
    path, RNS signed the DEFAULT 500 B MTU, the server confirmed 500 B and
    resources failed ("status: 07"). The establish path must wait, bounded,
    for a next-hop interface that exposes its hardware MTU.
    """

    seq = {"n": 0}
    waited = {"n": 0}

    def _fake_hw_mtu(_hash_bytes):
        seq["n"] += 1
        if seq["n"] < 3:
            return None
        return 8192

    real_transport = RNS.Transport
    real_time = kc.time
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: waited.__setitem__("n", waited["n"] + 1)
    patched = type("ShimTransport", (), {})()
    patched.next_hop_interface_hw_mtu = staticmethod(_fake_hw_mtu)
    RNS.Transport = patched
    kc.time = shim_time
    try:
        ok = kc.sHadesClientApp._wait_path_mtu(bytes.fromhex("11" * 16),
                                               tries=5)
    finally:
        RNS.Transport = real_transport
        kc.time = real_time

    assert ok, "live path existed after 3 polls; wait gave up early"
    assert seq["n"] == 3, seq
    assert waited["n"] == 2, waited


def test_wait_path_mtu_bounded_gives_up():
    """No path ever arrives -> bounded wait, then False (never worse than
    the old unconditional establish; the establish attempts still run)."""

    waited = {"n": 0}

    def _no_path(_h):
        return None

    real_transport = RNS.Transport
    real_time = kc.time
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: waited.__setitem__("n", waited["n"] + 1)
    patched = type("ShimTransport", (), {})()
    patched.next_hop_interface_hw_mtu = staticmethod(_no_path)
    RNS.Transport = patched
    kc.time = shim_time
    try:
        ok = kc.sHadesClientApp._wait_path_mtu(bytes.fromhex("22" * 16),
                                               tries=3)
    finally:
        RNS.Transport = real_transport
        kc.time = real_time

    assert ok is False, "no path arrived"
    assert waited["n"] == 3, waited


def test_bootlog_export_contract():
    """v0.36.2: the boot log mirrors into the user-visible Downloads
    collection (MediaStore on API 29+, public dir API 26-28) so crash
    triage needs no adb; a CLEAN boot must also produce a copy (the
    v0.36.1 device miss: only milestone/crash fires, both absent on a
    healthy boot). Java-side copy only (v0.15 jarray lesson)."""
    main_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    client_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py")
    main_src = open(main_path, encoding="utf-8").read()
    client_src = open(client_path, encoding="utf-8").read()

    assert "def _export_bootlog_for_user" in main_src, "export fn exists"
    assert "MediaStore$Downloads" in main_src, "API 29+ route"
    assert "external_primary" in main_src, \
        "literal volume name (jnius resolves only nested-class fields)"
    assert "VOLUME_EXTERNAL_PRIMARY" not in main_src, \
        "the parent-class field lookup threw on tay's device (log)"
    assert "_EXPORT_MIN_INTERVAL" in main_src and \
        "_EXPORT_IN_FLIGHT_TTL" in main_src and \
        "_stamp_path" in main_src and "_read_stamp" in main_src and \
        "_write_stamp" in main_src and "+ \".export\"" in main_src, \
        "shared stamp-file throttle + guard knobs (cross-instance state)"
    assert "DIRECTORY_DOWNLOADS" in main_src, "API 26-28 fallback route"
    assert "_display_name LIKE 'sHades_boot%.log'" in main_src, \
        "export-family row search: bare + stamped names, newest first"
    assert "sHades_boot_%s.log" in main_src and "%Y%m%d_%H%M%S" \
        in main_src and "time.gmtime" in main_src, \
        "timestamped insert name cannot collide with a foreign orphan"
    # v0.36.4: nio copy -- Files.copy(Path, OutputStream) is the legal
    # overload; FileInputStream/InputStream targets never resolved
    assert "java.nio.file.Paths" in main_src and \
        "java.nio.file.Files" in main_src and \
        ".copy(p9.get(src_path), out)" in main_src, \
        "nio copy on the legal overload"
    # the crash path mirrors the fatal log even when rendering is dead
    tb_idx = main_src.index("--- TRACEBACK ---")
    crash_hook = main_src.index("_export_bootlog_for_user()", tb_idx)
    assert crash_hook < main_src.index("sys.exit(1)", tb_idx), \
        "crash hook before exit"
    # v0.36.2: tee-time fire -- a clean boot leaves a copy too
    tee_idx = main_src.index("    _install_tee()\n")
    boot_export = main_src.index("_export_bootlog_for_user()", tee_idx)
    assert boot_export < main_src.index('print("PYTHON: main() starting'
                                        '"', tee_idx), \
        "first mirror runs before app import"
    # milestones refresh the mirror while BOOT_DEBUG is on, and the
    # v0.36.2 whoami-success mirror is the second call site
    assert client_src.count("from main import _export_bootlog_for_user"
                            ) == 2, "milestone + whoami-success mirrors"
    # degraded bridge never raises and never returns True off-device
    import main as mc
    assert mc._export_bootlog_for_user() is False, "dev no-op"
    print("PASS  bootlog export contract")


def test_whoami_retry_contract():
    """v0.36.2: an early whoami failure must not permanently cost the
    admin menu (the device miss: server-config item gated on
    app.i_am_admin, set only by a successful whoami). Bounded retry,
    knobs shrinkable in lanes; a whoami success mirrors the log."""
    import time as _time
    import kivy.clock as kclock
    client_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py")
    client_src = open(client_path, encoding="utf-8").read()
    assert "_WHOAMI_RETRY_N = 2" in client_src and \
        "_WHOAMI_RETRY_S = 4.0" in client_src, "knobs live in the class"
    assert client_src.count("whoami failed:") == 1 and \
        "tries_left - 1" in client_src, "bounded schedule, no hard loop"
    # the whoami verdict (incl. user_hash) rides the boot log so a Downloads
    # copy answers a missing admin item without adb (v0.36.3)
    assert "whoami ok: admin=%s" in client_src and \
        '"user_hash=%s"' in client_src, "verdict print ships"

    import kivy_client as kc
    app = kc.sHadesClientApp()

    class FlakyApi:
        def __init__(self, n_fail):
            self.n = 0
            self.n_fail = n_fail

        def whoami(self):
            self.n += 1
            if self.n <= self.n_fail:
                raise RuntimeError("net down (test)")
            return {"ok": 1, "is_admin": True, "is_moderator": True,
                    "roles": [],
                    "guild": {"name": "T", "icon": "", "banner": ""}}

    app._WHOAMI_RETRY_S = 0.05
    api = FlakyApi(2)
    app.guild_api = api
    app._fetch_whoami()
    i = 0
    while api.n < 3 and i < 40:
        _time.sleep(0.05)
        kclock.Clock.tick()
        i += 1
    assert api.n == 3, "all attempts fired (got %d)" % api.n
    assert app.i_am_admin is True, "flag lands after the late success"

    api2 = FlakyApi(99)
    app.guild_api = api2
    app._fetch_whoami()
    i = 0
    while api2.n < 3 and i < 40:
        _time.sleep(0.05)
        kclock.Clock.tick()
        i += 1
    _time.sleep(0.2)
    kclock.Clock.tick()
    assert api2.n == 3, "permanent failure still bounded at 3 (got %d)" \
        % api2.n
    print("PASS  whoami retry contract")


def _fake_jnius_device(main_mod, tmp_log):
    """Install a fake jnius module modelling the p4a bridge (API 29+):
    MediaStore insert/query, ContentResolver streams, and the REAL
    java.nio Files.copy(Path, OutputStream) overload -- every call's
    argument types are captured so the lane proves the bridge shape,
    not just source strings. Files.copy raises if handed the illegal
    (InputStream, OutputStream) pair, like real Java does."""
    import types
    import sys as _sys
    calls = []

    class FakeOut:
        def __init__(self):
            self.buf = []

        def write(self, b):
            self.buf.append(b)

        def close(self):
            pass

    class FakePath:
        pass

    class FakeQueryCursor:
        def __init__(self, has_row):
            self.has_row = has_row

        def moveToFirst(self):
            return self.has_row

        def getLong(self, _ix):
            return 77

        def close(self):
            pass

    class FakeResolver:
        def __init__(self):
            self.has_row = False

        def query(self, coll, proj, sel, args, order):
            calls.append(("query", sel))
            return FakeQueryCursor(self.has_row)

        def insert(self, coll, cv):
            calls.append(("insert", dict(cv)))
            self.has_row = True
            return "uri://dl/1"

        def openOutputStream(self, uri):
            calls.append(("openout", uri))
            return FakeOut()

    class ContentVal(dict):
        def put(self, k, v):
            self[k] = v

    class Files:
        @staticmethod
        def copy(src, out):
            if getattr(main_mod, "_FAKE_COPY_RAISES", None):
                raise RuntimeError("mid-copy native death (in-flight lane)")
            if isinstance(src, FakeOutputStream):
                raise TypeError(
                    "Files.copy(InputStream, OutputStream) is not an "
                    "overload -- exactly the shipped bug")
            assert isinstance(src, FakePath), "source must be a Path"
            out.write(open(src.name, "rb").read())
            calls.append(("copy", "path,out"))
            return src

    class FakeOutputStream:
        pass

    class Paths:
        @staticmethod
        def get(name):
            assert isinstance(name, str)
            p = FakePath()
            p.name = name
            return p

    class DL:
        def __init__(self):
            raise AssertionError("Downloads is called as a class")

        @staticmethod
        def getContentUri(vol):
            calls.append(("coll", vol))
            assert vol == "external_primary", (
                "parent-class field resolution -- literal volume name, "
                "jnius finds only nested-class-declared fields")
            return "coll://dl"

    class CUris:
        @staticmethod
        def withAppendedId(coll, row_id):
            assert row_id == 77
            return "uri://dl/%d" % row_id

    class PyAct:
        resolver = FakeResolver()
        mActivity = types.SimpleNamespace(
            getContentResolver=lambda: PyAct.resolver)

    class Ver:
        SDK_INT = 34

    def autoclass(name):
        calls.append(("ac", name))
        return {"android.provider.MediaStore$Downloads": DL,
                "org.kivy.android.PythonActivity": PyAct,
                "android.os.Build$VERSION": Ver,
                "android.content.ContentValues": ContentVal,
                "android.content.ContentUris": CUris,
                "java.nio.file.Paths": Paths,
                "java.nio.file.Files": Files}[name]

    fake = types.ModuleType("jnius")
    fake.autoclass = autoclass
    _sys.modules["jnius"] = fake
    return calls, FakeOut


def test_bootlog_bridge_contract():
    """v0.36.4: the whole MediaStore bridge executes against a fake
    jnius -- insert shape, Paths.get source, Files.copy(Path, out),
    and the copied bytes land in the fake stream. This lane executes
    what the static contract only texted: an illegal overload pair
    raises here instead of silently returning False on-device."""
    import main as main_mod
    calls, _fakeout = _fake_jnius_device(main_mod, None)

    def stamp_stale():
        """Drop the shared stamp: the next attempt must pass the guard."""
        try:
            os.unlink(main_mod._stamp_path())
        except OSError:
            pass

    try:
        stamp_stale()  # order-independent lane
        with open(main_mod._log_path(), "w", encoding="utf-8") as f:
            f.write("== boot v0.36.4 ==\n")
        assert main_mod._export_bootlog_for_user() is True, \
            "clean boot export lands on-device"
        kinds = [c[0] for c in calls]
        assert "insert" in kinds, "no existing row -> insert"
        # tay 2026-11-25 (log_13): orphan world -- with no row anywhere,
        # the insert name is timestamped, so it cannot collide with the
        # bare-name orphan file that made "Failed to build unique file"
        # permanent (3 misses out of 3 export attempts on that boot)
        import re as _re
        ins_names = [c[1]["_display_name"] for c in calls
                     if c[0] == "insert"]
        assert ins_names and _re.fullmatch(
            r"sHades_boot_\d{8}_\d{6}\.log", ins_names[0]), \
            "no-row insert name is stamped, collision-free: %s" % ins_names
        assert ("copy", "path,out") in calls, \
            "copy ran on the legal Path,OutputStream overload"
        ac_names = [c[1] for c in calls if c[0] == "ac"]
        assert "java.nio.file.Paths" in ac_names and \
            "java.nio.file.Files" in ac_names, "nio classes used"
        # overwrite lane: second run updates the known row
        calls.clear()
        stamp_stale()  # fresh attempt window
        with open(main_mod._log_path(), "a", encoding="utf-8") as f:
            f.write("second boot\n")
        assert main_mod._export_bootlog_for_user() is True
        assert ("query", "_display_name LIKE 'sHades_boot%.log'") in calls, \
            "overwrite query before any insert"
        assert [c[0] for c in calls].count("insert") == 0, \
            "no (1).log stacking: query found the row, insert skipped"
        # the success above left a fresh stamp: with no reset, the next
        # attempt is throttled -- proving the stamp really gates
        calls.clear()
        main_mod._EXPORT_MIN_INTERVAL = 60.0
        assert main_mod._export_bootlog_for_user() is False, \
            "a just-written stamp throttles without any reset"
        bridge_calls = [c for c in calls if c[0] in
                        ("coll", "query", "insert", "openout", "copy")]
        assert bridge_calls == [], \
            "no MediaStore traffic while throttled: %s" % bridge_calls
        main_mod._EXPORT_MIN_INTERVAL = 0.0
        stamp_stale()
        # tay's 0.36.22 crash (2026-11-20): a slow boot clears the
        # 30 s throttle and the second export from a _boot_file write
        # died inside the native copy, mid-stream. The in-flight guard
        # is the fix: while one native copy runs, every other attempt
        # exits before ANY bridge call, and the flag always clears.
        main_file = open(main_mod.__file__,
                         encoding="utf-8").read()
        assert "_EXPORT_IN_FLIGHT_TTL" in main_file and \
            "_write_stamp" in main_file and "_read_stamp" in main_file, \
            "shared stamp guard exists"
        calls.clear()
        main_mod._write_stamp(True)
        assert main_mod._export_bootlog_for_user() is False, \
            "in-flight export refuses before the bridge"
        hot = [c for c in calls if c[0] in
               ("coll", "query", "insert", "openout", "copy")]
        assert hot == [], \
            "no bridge traffic while a copy is in flight"
        # TTL: a hard process death mid-copy leaves the in-flight bit
        # written; the stamp must age out or exports lock out forever
        with open(main_mod._stamp_path(), "w", encoding="utf-8") as f:
            f.write("%.3f 1\n" % (main_mod.time.time() - 120.0))
        calls.clear()
        assert main_mod._export_bootlog_for_user() is True, \
            "TTL: an orphaned in-flight stamp cannot lock out forever"
        assert main_mod._read_stamp()[1] is False, \
            "the TTL pass rewrites the stamp not-in-flight (finally)"
        calls.clear()
        stamp_stale()
        assert main_mod._export_bootlog_for_user() is True, \
            "no stamp -> copy runs again"
        assert main_mod._read_stamp()[1] is False, \
            "success clears the in-flight bit (finally)"
        calls.clear()
        stamp_stale()
        main_mod._FAKE_COPY_RAISES = [True]
        try:
            r = main_mod._export_bootlog_for_user()
        finally:
            main_mod._FAKE_COPY_RAISES[0] = False
        assert r is False and main_mod._read_stamp()[1] is \
            False, "copy failure still clears the in-flight bit (finally)"
    finally:
        import sys as _sys
        del _sys.modules["jnius"]
    print("PASS  bootlog bridge contract")


def test_bootlog_clipboard_contract():
    """v0.36.5: the diagnosis channel cannot ride the bridge it
    diagnoses -- an ungated clipboard item in BOTH long-press menus
    hands the boot-log text over the proven kivy bridge, and the
    export prints its stage story into the tee (visible in the very
    paste it enables)."""
    with open("kivy_client.py", "r", encoding="utf-8") as f:
        client_src = f.read()
    with open("main.py", "r", encoding="utf-8") as f:
        main_src = f.read()
    n_items = client_src.count('"copy boot log"')
    assert "def _copy_bootlog" in client_src and n_items == 2, (
        "ungated item x%d + method (both menus)" % n_items)
    assert "def _copy_bootlog" in client_src, "handoff method ships"
    assert "boot log handed to clipboard" in client_src, (
        "handoff prints its size into the tee")
    assert "export: out-stream opened" in main_src, (
        "stage print: stream open")
    assert "export: nio copy ok" in main_src, (
        "stage print: copy ok")
    print("PASS  bootlog clipboard contract")


def test_softinput_bridge_contract():
    """v0.36.11->v0.36.13: main._android_softinput_resize() executes
    against a fake jnius and the argument TYPES are the contract.
    AOSP receipts: WindowManager.java defines SOFT_INPUT_ADJUST_RESIZE
    = 0x10 inside the LayoutParams nested class (exact-class field per
    the jnius law) and Window.java's setSoftInputMode(int mode) is the
    only overload -- Java takes one int, nothing else. tay's 0.36.11
    boot receipt: the direct call from main()'s SDL thread died on
    ViewRootImpl$CalledFromWrongThreadException, so the set must ride
    Activity.runOnUiThread via a python-implemented java.lang.Runnable
    -- the lane executes that queued runnable itself and the type
    contract applies to what the UI thread would run."""
    import io
    import sys as _sys
    import types as _t
    import main as main_mod
    import contextlib as _ctx

    calls = []

    class FakeWin:
        def setSoftInputMode(self, mode):
            if not isinstance(mode, int) or isinstance(mode, bool):
                raise TypeError("Window.setSoftInputMode(int) -- one "
                                "int overload, AOSP Window.java:1223")
            calls.append(("setmode", mode))

    def _win():
        return _WIN["win"]

    _WIN = {"win": FakeWin()}

    class PyAct:
        mActivity = _t.SimpleNamespace(
            getWindow=lambda: _win(),
            runOnUiThread=lambda r: calls.append(("queue", r)))

    class FakeWSLP:
        def __init__(self):
            raise AssertionError("LayoutParams is read as a class")

    # the real declaration placement: the constant lives ON LayoutParams
    FakeWSLP.SOFT_INPUT_ADJUST_RESIZE = 0x10

    def autoclass(name):
        calls.append(("ac", name))
        return {"org.kivy.android.PythonActivity": PyAct,
                "android.view.WindowManager$LayoutParams":
                    FakeWSLP}[name]

    class FakePyJC:
        pass

    def java_method(sig):
        def deco(f):
            f._jnius_sig = sig
            return f
        return deco

    fake = _t.ModuleType("jnius")
    fake.autoclass = autoclass
    fake.PythonJavaClass = FakePyJC
    fake.java_method = java_method
    _sys.modules["jnius"] = fake
    try:
        buf = io.StringIO()
        with _ctx.redirect_stdout(buf):
            main_mod._android_softinput_resize()
        out = buf.getvalue()
        queued = [c[1] for c in calls if c[0] == "queue"]
        assert len(queued) == 1, \
            "the set dispatches once via runOnUiThread: %s" % calls
        runnable = queued[0]
        assert isinstance(runnable, FakePyJC), \
            "the runnable is a python-implemented jnius proxy: %r" % runnable
        assert getattr(type(runnable), "__javainterfaces__", None) == \
            ["java/lang/Runnable"], \
            "the proxy implements java/lang/Runnable: %r" % \
            getattr(type(runnable), "__javainterfaces__", None)
        run_fn = getattr(runnable, "run", None)
        assert callable(run_fn) and getattr(run_fn, "_jnius_sig", None) == \
            "()V", "run carries the java ()V signature"
        # run the queued runnable under capture: on device this leg
        # executes ON the UI thread; its receipt names that leg
        buf_run = io.StringIO()
        with _ctx.redirect_stdout(buf_run):
            run_fn()
        assert "SOFTINPUT: window set to ADJUST_RESIZE" in \
            buf_run.getvalue(), \
            "the UI-thread leg prints its set receipt: %r" % buf_run.getvalue()
        assert [c for c in calls if c[0] == "setmode"] == [("setmode", 0x10)], \
            "the UI-thread call lands as the AOSP int: %s" % calls
        acs = [c[1] for c in calls if c[0] == "ac"]
        assert acs == ["org.kivy.android.PythonActivity",
                       "android.view.WindowManager$LayoutParams"], \
            "exact classes only: %s" % acs
        assert "SOFTINPUT: dispatch queued on ui thread" in out, repr(out)
        # type-faithfulness leg: a misdeclared constant (str where the
        # int overload expects a mode) names itself in the production
        # except BEFORE any dispatch instead of dying silently or lying
        # about the receipt
        calls.clear()
        FakeWSLP.SOFT_INPUT_ADJUST_RESIZE = "16"
        buf = io.StringIO()
        with _ctx.redirect_stdout(buf):
            main_mod._android_softinput_resize()
        assert [c for c in calls if c[0] in ("setmode", "queue")] == [], \
            "no dispatch on a str mode: %s" % calls
        assert "SOFTINPUT: resize not dispatched" in buf.getvalue(), \
            repr(buf.getvalue())
    finally:
        import sys as _sys
        del _sys.modules["jnius"]
    print("PASS  softinput bridge contract")


def test_softinput_frame_meter_contract():
    """v0.36.15: the frame meter polls from the KIVY thread. tay's log
    30 (launches 1795169468/71s) killed the v0.36.14 design: the
    OnGlobalLayoutListener ran python on the UI thread on every layout
    pass and collided with the main thread mid-import of kivy's pyx
    machinery -- the process died inside `import kivy` with no
    traceback. So: main.py must carry NO ui-thread listener at all,
    and the client tick must measure directly -- first tick resolves
    jnius handles and prints the live receipt, later ticks turn
    base - bottom (largest bottom seen vs latest; the IME is up when
    the bottom drops) into chat container bottom padding, and a
    failing read reports exactly once."""
    import io
    import sys as _sys
    import types as _t
    import contextlib as _ctx
    import unittest.mock as _mock
    import main as main_mod
    import kivy_client as kc

    src = open(main_mod.__file__, encoding="utf-8").read()
    # code markers only: prose comments may document the retired design
    assert "addOnGlobalLayoutListener" not in src and \
        "ViewTreeObserver$OnGlobalLayoutListener" not in src, \
        "the v0.36.14 boot-killer listener must be retired from main.py"
    assert "runOnUiThread" in src, \
        "the window-mode dispatch (0.36.13) still rides the ui thread"

    state = {"frames": []}

    class FakeRect:
        def __init__(self):
            self.bottom = 0

    class FakeDecor:
        def getWindowVisibleDisplayFrame(self, rect):
            rect.bottom = state["frames"][-1] if state["frames"] else 0

    _activity = _t.SimpleNamespace(
        getWindow=lambda: _t.SimpleNamespace(
            getDecorView=lambda: FakeDecor()))
    FakePythonActivity = _t.SimpleNamespace(mActivity=_activity)

    def autoclass(name):
        if name == "org.kivy.android.PythonActivity":
            return FakePythonActivity
        if name == "android.graphics.Rect":
            return FakeRect
        raise ValueError("unexpected autoclass %s" % name)

    fake = _t.ModuleType("jnius")
    fake.autoclass = autoclass
    _sys.modules["jnius"] = fake
    prev_state = dict(main_mod._SOFTINPUT)
    prev_meter = kc._FRAME_METER
    prev_reported = set(kc._FRAME_REPORTED)
    try:
        class FakeCS:
            padding = [0, 0, 0, 0]

        class FakeScreen:
            ids = {"chat_screen": FakeCS}

        class FakeRoot:
            def get_screen(self, name):
                if name != "main":
                    raise KeyError(name)
                return FakeScreen

        app = _t.SimpleNamespace(root=FakeRoot())
        buf = io.StringIO()
        with _mock.patch("kivy.utils.platform", "android"):
            with _ctx.redirect_stdout(buf):
                # first tick: resolve, live receipt, closed-keyboard
                # reading (2112) establishes the base
                state["frames"].append(2112)
                kc.sHadesClientApp._softinput_tick(app)
                assert getattr(kc, "_FRAME_METER") is not None, \
                    "the first tick resolves the jnius handles"
                assert "SOFTINPUT: frame meter live (kivy thread " \
                       "poll)" in buf.getvalue()
                assert main_mod.softinput_base() == 2112
                assert main_mod.softinput_live() is True
                assert FakeCS.padding == [0, 0, 0, 0]
                # the IME opens: the meter reads the drop and lifts
                state["frames"].append(1700)
                kc.sHadesClientApp._softinput_tick(app)
                assert main_mod.softinput_bottom() == 1700
                assert FakeCS.padding == [0, 0, 0, 412], \
                    "the composer group lifts by the IME height: %r" % \
                    (FakeCS.padding,)
                # the IME closes: the lift clears
                state["frames"].append(2112)
                kc.sHadesClientApp._softinput_tick(app)
                assert FakeCS.padding == [0, 0, 0, 0], \
                    "the lift clears when the keyboard closes: %r" % \
                    (FakeCS.padding,)
            # a failing read reports exactly once, then stops spamming
            boom = _t.ModuleType("jnius")

            def broken(name):
                raise ValueError("dead jnius")

            boom.autoclass = broken
            _sys.modules["jnius"] = boom
            kc._FRAME_METER = None
            kc._FRAME_REPORTED.clear()
            seen = []
            with _mock.patch.object(kc, "_boot_file",
                                    side_effect=seen.append):
                kc.sHadesClientApp._softinput_tick(app)
                kc.sHadesClientApp._softinput_tick(app)
            assert len(seen) == 1 and "frame read failed" in seen[0], \
                "a failing tick reports once, not per tick: %r" % (seen,)
    finally:
        import sys as _sys
        del _sys.modules["jnius"]
        kc._FRAME_METER = prev_meter
        kc._FRAME_REPORTED.clear()
        kc._FRAME_REPORTED.update(prev_reported)
        main_mod._SOFTINPUT.clear()
        main_mod._SOFTINPUT.update(prev_state)
    print("PASS  softinput frame meter (kivy-thread poll) + lift")


def test_site_editor_publish_contract():
    """P7c: the [ Edit page ] affordance reaches the micron editor with
    the page's source; the publish leg posts the edited source to the
    page the editor opened on and returns to the page on success; a
    server refusal stays on the editor with the server error visible."""
    import base64

    cid, path = "c" * 64, "/index.mu"
    source = "#!c=0\n> Owned page\n\nplain line\n"

    class _NowClock(object):
        @staticmethod
        def schedule_once(fn, dt=0):
            fn(0)

    class _InlineThread(object):
        def __init__(self, target=None, daemon=False, **k):
            self._target = target

        def start(self):
            self._target()

    class _FakeApi(object):
        def __init__(self, res):
            self.calls = []
            self.res = res

        def site_put(self, channel, pth, data_b64, mime=None):
            self.calls.append((channel, pth, data_b64, mime))
            return self.res

    def _screen():
        return types.SimpleNamespace(
            _src=types.SimpleNamespace(text=""),
            _status=types.SimpleNamespace(text=""),
            _prev_holder=types.SimpleNamespace(
                clear_widgets=lambda: None,
                parent=types.SimpleNamespace(scroll_y=1.0)),
            )
    scr = _screen()
    root = types.SimpleNamespace(current="main")

    def get_screen(name):
        if name == "site_editor":
            return scr
        raise KeyError(name)
    root.get_screen = get_screen

    app = types.SimpleNamespace(root=root, guild_api=None,
                                _site_current=(cid, path),
                                _site_src_text=source,
                                _edit_cid=None, _edit_path=None,
                                _site_load_page=lambda c, p: None)

    # 1. the affordance ref opens the editor carrying the source
    app._open_site_editor = types.MethodType(
        kc.sHadesClientApp._open_site_editor, app)
    app._enter_site_editor = types.MethodType(
        kc.sHadesClientApp._enter_site_editor, app)
    kc.sHadesClientApp._micron_link_tapped(app, "shades:site-edit")
    assert root.current == "site_editor", root.current
    assert scr._src.text == source, scr._src.text[:60]
    assert getattr(app, "_edit_cid") == cid and \
        getattr(app, "_edit_path") == path

    # 2. publish posts the edited source and returns to the page
    api = _FakeApi({"ok": 1, "channel": cid, "path": path})
    reloaded_ = []
    app.guild_api = api
    app._site_load_page = lambda c, p: reloaded_.append((c, p))
    app._site_editor_publish = types.MethodType(
        kc.sHadesClientApp._site_editor_publish, app)
    app._site_editor_publish_done = types.MethodType(
        kc.sHadesClientApp._site_editor_publish_done, app)
    scr._src.text = "#!c=0\n> Edited\n"
    real_clock, real_thread = kc.Clock, kc.threading.Thread
    try:
        kc.Clock = _NowClock
        kc.threading.Thread = _InlineThread
        kc.sHadesClientApp._site_editor_publish(app)
        assert api.calls == [(cid, path, base64.b64encode(
            "#!c=0\n> Edited\n".encode("utf-8")).decode("ascii"), None)], api.calls
        assert root.current == "main" and reloaded_ == [(cid, path)], \
            (root.current, reloaded_)
        assert scr._status.text == "published", scr._status.text

        # 3. a refusal stays on the editor with the server error visible
        api2 = _FakeApi({"ok": 0, "error": "site edit permission required",
                         "code": 403})
        app.guild_api = api2
        reloaded_[:] = []
        root.current = "site_editor"
        kc.sHadesClientApp._site_editor_publish(app)
        assert api2.calls and api2.calls[0][0] == cid
        assert root.current == "site_editor", "a refusal must not leave the editor"
        assert not reloaded_, "no reload after a refusal"
        assert "publish failed" in scr._status.text \
            and "site edit" in scr._status.text, scr._status.text
    finally:
        kc.Clock = real_clock
        kc.threading.Thread = real_thread
    print("PASS  site editor (affordance -> publish -> refusal)")


def test_site_create_door_contract():
    """tay's device receipt (0.36.24): a permitted viewer on a site whose
    read 404s (seeded-less channel or deleted target) had no widget
    anywhere. The empty state IS the door now: the not-found read rides
    the server's can_edit verdict, the leg renders [ Create index page ],
    and the ref enters the editor with the starter template keyed to the
    not-found target. Transport errors and unauthorized viewers still
    see the plain empty page."""
    cid, path = "d" * 64, "/index.mu"

    class SitePage(object):
        def __init__(self):
            self.children = []

        def add_widget(self, w):
            self.children.append(w)

    def _main_root(page):
        def clear():
            page.children = []
        added = []
        grid = types.SimpleNamespace(
            clear_widgets=clear,
            add_widget=lambda w: added.append(w))
        return types.SimpleNamespace(
            current="main",
            get_screen=lambda name: types.SimpleNamespace(
                ids=types.SimpleNamespace(message_grid=grid),
                _current_channel=None)
            if name == "main" else
            (_ for _ in ()).throw(KeyError(name))), added

    def _screen():
        return types.SimpleNamespace(
            _src=types.SimpleNamespace(text=""),
            _status=types.SimpleNamespace(text=""),
            _prev_holder=types.SimpleNamespace(
                clear_widgets=lambda: None,
                parent=types.SimpleNamespace(scroll_y=1.0)),
            )
    scr = _screen()
    root = types.SimpleNamespace(current="main")

    def get_screen(name):
        if name == "site_editor":
            return scr
        if name == "main":
            return types.SimpleNamespace(
                ids=types.SimpleNamespace(
                    message_grid=types.SimpleNamespace(children=[])),
                _current_channel=None)
        raise KeyError(name)
    root.get_screen = get_screen

    app = types.SimpleNamespace(
        root=root, guild_api=None,
        _site_current=(cid, path), _site_src_text=None,
        _msg_widgets=[], _remove_skeleton=lambda *a, **k: None,
        _set_status=lambda *a, **k: None,
        _edit_cid=None, _edit_path=None,
        site_pages=None, sitemsg_widgets=None)
    app._site_empty_page = types.MethodType(kc.sHadesClientApp._site_empty_page, app)
    app._open_new_site_page = types.MethodType(
        kc.sHadesClientApp._open_new_site_page, app)
    app._set_status = types.MethodType(kc.sHadesClientApp._set_status, app)
    app._micron_label = types.MethodType(
        kc.sHadesClientApp._micron_label, app)

    # 1. not-found + permitted -> the door
    page = SitePage()
    root, added = _main_root(page)
    app.root = root
    kc.sHadesClientApp._site_empty_page(app, cid, "resource not found",
                                        can_edit=True)
    assert added and type(added[0]).__name__ == "SitePage", added
    texts = [w.text for w in added[0].children]
    assert any("[ Create index page ]" in t for t in texts), texts
    assert any("site-create" in t for t in texts), texts
    assert any("This site is empty" in t for t in texts), texts

    # 2. transport error + permitted -> still the plain empty page
    page2 = SitePage()
    root, added = _main_root(page2)
    app.root = root
    kc.sHadesClientApp._site_empty_page(app, cid, "no response",
                                        can_edit=True)
    assert not any("site-create" in w.text for w in added[0].children), \
        "the door must not open on a transport error"

    # 3. not-found + unauthorized -> plain empty page
    page3 = SitePage()
    root, added = _main_root(page3)
    app.root = root
    kc.sHadesClientApp._site_empty_page(app, cid, "resource not found",
                                        can_edit=False)
    assert not any("site-create" in w.text for w in added[0].children)

    # 4. the door ref enters the editor with the starter template
    root, added = _main_root(SitePage())
    app.root = root
    app.SITE_STARTER_INDEX_MU = kc.sHadesClientApp.SITE_STARTER_INDEX_MU
    root.get_screen = (lambda name, _r=root: scr
                       if name == "site_editor" else _r.get_screen(name))
    root.current = "main"
    app._site_current = (cid, path)
    kc.sHadesClientApp._micron_link_tapped(app, "shades:site-create")
    assert root.current == "site_editor", root.current
    assert scr._src.text == kc.sHadesClientApp.SITE_STARTER_INDEX_MU, \
        scr._src.text[:60]
    assert app._edit_cid == cid and app._edit_path == path, \
        (app._edit_cid, app._edit_path)
    assert "creating" in scr._status.text, scr._status.text
    print("PASS  site create door (verdict -> affordance -> editor)")


def test_motd_strip_contract():
    """P6: whoami's guild motd binds to the sidebar strip; a set motd
    yields a tappable strip opening the full-text popup, an empty one
    hides the strip. The kv carries the strip between the banner and
    the channel list."""
    app = types.SimpleNamespace(server_motd="")
    app._apply_motd = types.MethodType(kc.sHadesClientApp._apply_motd, app)
    app._build_motd_popup = types.MethodType(
        kc.sHadesClientApp._build_motd_popup, app)
    app.theme_colors = {"text": (0.92, 0.94, 0.96, 1)}
    kc.sHadesClientApp._apply_motd(app, "Welcome to the node")
    assert app.server_motd == "Welcome to the node", app.server_motd
    app._apply_motd("")
    assert app.server_motd == "", "empty motd must hide the strip"
    app._apply_motd("long motd: " + "x" * 200)

    popup = kc.sHadesClientApp._build_motd_popup(app)
    full_text = None
    for child in popup.content.children:
        for c in getattr(child, "children", []):
            if hasattr(c, "text") and "long motd" in c.text:
                full_text = c.text
    assert popup.title == "Server message", popup.title
    assert full_text and len(full_text) == len(app.server_motd), \
        "the popup must carry the FULL motd, untruncated"

    kvs = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kv_md.kv"),
        encoding="utf-8").read()
    assert "id: motd_strip" in kvs, "strip absent from kv_md.kv"
    assert "app.server_motd" in kvs, "strip does not bind app.server_motd"
    # the strip sits between the banner strip and the channel list
    assert kvs.index("id: banner_strip") < kvs.index("id: motd_strip") \
        < kvs.index("id: channel_grid"), "strip is out of position"
    print("PASS  motd strip (binds -> popup -> kv placement)")


def test_announce_composer_contract():
    """P8: the composer's truth per channel -- an announce channel
    without a grant read-locks the composer (hint + disabled binding on
    the kivy Properties), a holder sees the Announce label, and every
    other kind leaves the composer open. The channels apply stores the
    server's can_post stamp and old servers default it to True."""
    app = types.SimpleNamespace(
        composer_readonly=False,
        composer_send_label="Send",
    )
    ANNC_CID = "cid-annc"
    app._channel_kind = types.MethodType(kc.sHadesClientApp._channel_kind, app)
    SITE_CID = "cid-general"
    app._channel_kinds = {ANNC_CID: "announce", SITE_CID: "chat"}
    app._channel_can_post = {ANNC_CID: False}
    app._sync_composer_props = types.MethodType(
        kc.sHadesClientApp._sync_composer_props, app)

    app._sync_composer_props("announcements", ANNC_CID)
    assert app.composer_readonly is True, app.composer_readonly
    assert app.composer_send_label == "Announce", app.composer_send_label

    app._channel_can_post[ANNC_CID] = True
    app._sync_composer_props("announcements", ANNC_CID)
    assert app.composer_readonly is False, app.composer_readonly
    assert app.composer_send_label == "Announce", app.composer_send_label

    app._channel_kinds[SITE_CID] = "chat"
    app._sync_composer_props("general", SITE_CID)
    assert app.composer_readonly is False and \
        app.composer_send_label == "Send", \
        (app.composer_readonly, app.composer_send_label)

    # kv contract: the composer bindings carry the properties
    kvs = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kv_md.kv"),
        encoding="utf-8").read()
    assert "disabled: app.composer_readonly" in kvs, "input not read-locked"
    assert "app.composer_send_label" in kvs, "send label not mirrored"
    assert "'announcements only'" in kvs, "hint missing"

    # static: the client's can_post stamp store + the fetch guard
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py"),
        encoding="utf-8").read()
    assert "self._channel_can_post = {}" in src, "stamp store missing"
    assert 'c.get("can_post", True)' in src, "old-server default not True"
    assert 'not in ("chat", "announce")' in src, "fetch guard not widened"
    print("PASS  announce composer (stamp -> read-only -> label)")


def test_announce_fetch_guard_contract():
    """P8: announce channels keep the message ledger flow -- the fetch
    runs (guard passes) and the stream slot stays assigned for them."""
    import os
    # the guard is inline in _fetch_messages; assert its shape statically
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py"),
        encoding="utf-8").read()
    seg = src[src.index("def _fetch_messages"):]
    seg = seg[:seg.index("def ", 1)]
    assert 'not in ("chat", "announce")' in seg, \
        "the ledger fetch must run for announce channels"
    # and the stream slot must NOT release for announce
    seg2 = src[src.index("def _switch_channel"):]
    seg2 = seg2[:seg2.index("def ", 1)]
    assert 'kind not in ("chat", "announce")' in seg2, \
        "announce channels keep live push (the stream slot stays)"
    assert 'self._sync_composer_props(name, cid)' in seg2, \
        "channel switch never skips the composer sync"
    print("PASS  announce ledger flow (fetch + push + composer sync)")


def main():
    test_link_dead_reported_and_signalled()
    test_link_raise_reported_and_signalled()
    test_plain_object_link_untouched()
    test_heal_never_nulls_healthy_router()
    test_heal_rebuild_only_when_router_gone()
    test_blob_upload_retries_timeout_then_posts()
    test_blob_retry_never_inlines_big_blob_on_timeout()
    test_blob_linkdead_never_falls_through()
    test_wait_path_mtu_bounded_and_uses_live_path()
    test_wait_path_mtu_bounded_gives_up()
    test_bootlog_export_contract()
    test_whoami_retry_contract()
    test_bootlog_bridge_contract()
    test_bootlog_clipboard_contract()
    test_softinput_bridge_contract()
    test_softinput_frame_meter_contract()
    test_site_editor_publish_contract()
    test_site_create_door_contract()
    test_motd_strip_contract()
    test_announce_composer_contract()
    test_announce_fetch_guard_contract()
    print("PASS: 22  FAIL: 0")


if __name__ == "__main__":
    main()
