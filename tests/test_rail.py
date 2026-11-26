#!/usr/bin/env python3
"""Rail tests (Task 6) — rail buttons carry icons, menus, ingest flow.

Pins down:
  - the rail builds GuildRailButtons: first-letter tile without an icon,
    circular image once icon_path is set, active server highlighted;
  - _ingest_guild_icon: whoami's guild icon (a blob_id) -> blobs_get ->
    icons/<host>.<ext> on disk -> servers entry icon_path -> rail refresh,
    and no re-fetch when unchanged;
  - servers.json roundtrips icon_path;
  - the rail context menu: open / copy invite / server settings (admin only)
    / remove server.
"""
import os, sys, time, base64

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

def pump(seconds=1.5):
    """Drain the ingest worker threads + their scheduled Clock hops."""
    from kivy.clock import Clock
    deadline = time.time() + seconds
    while time.time() < deadline:
        Clock._process_events()
        Clock.tick()
        time.sleep(0.02)


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0

    PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(64)

    class FakeApi:
        def __init__(self):
            self.blob_calls = []
            self.data = {}

        def blobs_get(self, blob_id):
            self.blob_calls.append(blob_id)
            return {"ok": 1, "blob_id": blob_id, "total": len(PNG),
                    "offset": 0, "eof": True,
                    "data_b64": base64.b64encode(PNG).decode()}

        def blobs_get_all(self, blob_id):
            """Chunk 5 rides the assembled entry: record the fetch and
            hand back the whole asset in the single-shot shape."""
            self.blob_calls.append(blob_id)
            return {"ok": 1, "blob_id": blob_id, "total": len(PNG),
                    "offset": 0, "eof": True,
                    "data_b64": base64.b64encode(PNG).decode()}

        def blobs_put(self, name, mime, data):
            return {"ok": 1, "blob_id": "d" * 32, "size": len(data)}

    from kivy.lang import Builder
    try:
        Builder.load_file("kv_md.kv")
    except Exception:
        Builder.load_file(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "kv_md.kv"))

    # isolate the app data dir: the rail ingest writes icons + servers.json;
    # without this the real ~/.sHades_client leaks between runs and a stale
    # icon_path survives the _add_server preservation logic
    import tempfile
    data_base = tempfile.mkdtemp(prefix="rail_test_data_")
    kc._app_data_dir = lambda: os.path.join(data_base, "sHades_client")
    os.makedirs(kc._app_data_dir(), exist_ok=True)

    app = kc.sHadesClientApp()
    app.guild_api = FakeApi()
    app.i_am_admin = True
    hub = "127.0.0.1:29123"
    app._add_server(hub, "tess", display_name="Test Hub")
    srv = [s for s in app._servers if s.get("host") == "127.0.0.1"
           and s.get("port") == 29123][0]
    srv["display_name"] = "Test Hub"
    app._servers = [srv]
    app._active_server_idx = 0

    # ---------- rail without an icon: first-letter tile ----------
    from kivy.uix.screenmanager import ScreenManager
    from kivy.uix.screenmanager import Screen
    sm = ScreenManager()
    main_screen = kc.MainScreen(name="main")
    sm.add_widget(main_screen)
    app.root = sm
    app._refresh_server_sidebar()
    grid = sm.get_screen("main").ids.server_grid
    widgets = grid.children
    rail = [w for w in widgets if isinstance(w, kc.GuildRailButton)]
    check("rail builds GuildRailButton widgets", len(rail) == 1,
          [type(w).__name__ for w in widgets])
    check("no icon -> first-letter tile", rail and rail[0].letter_tile
          and rail[0].letter_tile.text == "T",
          dict(tile=type(rail[0].letter_tile).__name__ if rail else None))
    check("active server carries the highlight", rail and rail[0].active)
    check("rail button carries the display name",
          rail and rail[0].display_name == "Test Hub",
          rail[0].display_name if rail else "?")

    # ---------- ingest: whoami icon -> disk -> rail ----------
    blob_id = "d" * 32
    app._ingest_guild_icon(blob_id, srv)
    pump()
    icon_path = srv.get("icon_path")
    check("icon_path lands in the servers entry", bool(icon_path), srv)
    check("icon bytes on disk",
          icon_path and os.path.isfile(icon_path)
          and open(icon_path, "rb").read() == PNG,
          icon_path)
    saved = kc.json.load(open(app._servers_path))
    entry = [s for s in saved if s.get("host") == srv["host"]][0]
    check("servers.json roundtrips icon_path", entry.get("icon_path"),
          entry.get("icon_path"))

    rail2 = [w for w in grid.children if isinstance(w, kc.GuildRailButton)]
    check("rail refreshes to show the icon",
          rail2 and rail2[0].icon_widget is not None
          and rail2[0].icon_widget.source == icon_path,
          dict(src=rail2[0].icon_widget.source if rail2 else None))

    # ---------- unchanged icon: no re-fetch ----------
    calls_before = len(app.guild_api.blob_calls)
    app._ingest_guild_icon(blob_id, srv)
    pump()
    check("no refetch when the icon is unchanged",
          len(app.guild_api.blob_calls) == calls_before,
          app.guild_api.blob_calls)

    # ---------- changed icon: refetch ----------
    app.guild_api.blobs_get = (lambda bid: {"ok": 1, "blob_id": bid,
                                            "eof": True,
                                            "data_b64": base64.b64encode(
                                                PNG).decode()})
    app._ingest_guild_icon("e" * 32, srv)
    pump()
    check("changed icon refetches",
          srv.get("icon_path", "").endswith("e" * 32 + ".png")
          or srv.get("icon_path") != icon_path,
          dict(old=icon_path, new=srv.get("icon_path")))

    # ---------- context menu ----------
    client = app
    actions = rail2[0].build_context_menu()
    labels = [a[0] for a in actions]
    check("menu offers open + copy invite",
          any("open" in l for l in labels)
          and any("invite" in l for l in labels), labels)
    check("menu offers server settings for admin",
          any("settings" in l for l in labels), labels)
    app.i_am_admin = False
    actions = rail2[0].build_context_menu()
    labels = [a[0] for a in actions]
    check("non-admin loses the settings item",
          not any("server settings" in l for l in labels), labels)
    # profile config (tabbed: appearance + voice) reaches every user; the
    # config screen is its only route (device log 15: the tabs existed
    # but nothing opened the screen after the standalone entry was removed)
    check("profile config reaches every user",
          any("profile config" in l for l in labels), labels)
    sm.add_widget(kc.ConfigScreen(name="config"))
    entry = next(a for a in actions if a[0] == "profile config")
    entry[2]()
    check("profile config entry lands on the config screen",
          sm.current == "config", sm.current)
    sm.current = "main"
    check("non-admin keeps remove server",
          any("remove" in l for l in labels), labels)
    app.i_am_admin = True

    # ---------- icon helper tolerant of no guild ----------
    app.guild_api = None
    ok = True
    try:
        app._ingest_guild_icon("d" * 32, srv)
        pump()
    except Exception as e:
        ok = False
    check("ingest without a guild connection is a no-op", ok, "")

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
