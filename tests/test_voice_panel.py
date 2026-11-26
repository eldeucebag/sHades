#!/usr/bin/env python3
"""Voice panel tests (build-order item 6): rail rows + join panel.

Pins down:
  - on_voice_state stores the room roster and feeds the joined room's
    engine.on_roster (the engine's dialer picks peer dests off it);
  - the rail: a voice-kind channel builds its VoiceRow adjacent to the
    ChannelButton; the row renders member count + names + join/leave;
  - the raw k:"voice" push frame routes into engine.packet_relayed;
  - guild_api.voice_publish b64-encodes the engine's raw packet.
"""
import os
import sys
import time

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""), flush=True)


def pump(seconds=0.4):
    from kivy.clock import Clock
    deadline = time.time() + seconds
    while time.time() < deadline:
        Clock._process_events()
        Clock.tick()
        time.sleep(0.02)


def build_app():
    from kivy.lang import Builder
    for path in ("kv_md.kv",
                 os.path.join(os.path.dirname(
                     os.path.dirname(os.path.abspath(__file__))),
                     "kv_md.kv")):
        try:
            Builder.load_file(path)
            break
        except Exception:
            pass
    import kivy_client as kc
    from kivy.uix.screenmanager import ScreenManager
    app = kc.sHadesClientApp()
    import kivy.app as _kapp

    class _RA:
        def __init__(self, a):
            self._a = a

        def __getattr__(self, name):
            return getattr(self._a, name)

    _kapp.App._running_app = _RA(app)
    sm = ScreenManager()
    sm.add_widget(kc.MainScreen(name="main"))
    app.root = sm
    pump()
    return app, sm.get_screen("main")


class FakeEngine:
    def __init__(self):
        self.rosters = []
        self.relayed = []

    def on_roster(self, cid, members):
        self.rosters.append((cid, list(members)))

    def packet_relayed(self, dest, body):
        self.relayed.append((dest, bytes(body)))

    def set_muted(self, m):
        self.muted = m


class FakeApi:
    def voice_join(self, channel, dest, muted=False):
        self.join_seen = (channel, dest, muted)
        return {"ok": 1, "channel": channel,
                "members": [{"viewer": "BBBB"*8, "dest": "b"*32,
                             "muted": False, "at": 1.0}]}

    def voice_leave(self):
        return {"ok": 1}


def main():
    app, main_screen = build_app()
    app.guild_api = FakeApi()
    app.my_user_hash = "a" * 64
    app._set_status = lambda s: None

    check("voice state initialized", hasattr(app, "_voice_rooms")
          and hasattr(app, "_voice_engine"))

    # voice rows land adjacent to the voice-kind channel's button
    app._channels = {"lounge": "v" * 32, "general": "g" * 32}
    app._channel_kinds = {"v" * 32: "voice", "g" * 32: "chat"}
    app._channel_pos = {"lounge": 10, "general": 20}
    app._add_channel("general")
    app._add_channel("lounge")
    grid = main_screen.ids.channel_grid
    from client_widgets import VoiceRow, ChannelButton
    rows = [w for w in grid.children if isinstance(w, VoiceRow)]
    check("voice row built for voice-kind channel", len(rows) == 1,
          "rows=%d" % len(rows))
    btns = [w for w in grid.children
            if isinstance(w, ChannelButton) and w.channel_name == "lounge"]
    check("channel button kept for navigation", len(btns) == 1)
    if rows:
        # adjacency: directly before or after the button's child index
        b_idx = grid.children.index(btns[0])
        r_idx = grid.children.index(rows[0])
        check("voice row adjacent to its button", abs(b_idx - r_idx) == 1,
              "btn=%d row=%d" % (b_idx, r_idx))
        row = rows[0]
        row.render([{"viewer": "AAAA"*8, "dest": "a"*32, "muted": False},
                    {"viewer": "BBBB"*8, "dest": "b"*32, "muted": False}],
                   joined=False)
        check("row renders member count", "2 in call" in row._label.text,
              row._label.text)
        check("row shows join", row._cta.text == "join")

    # on_voice_state: stores roster + does not touch a foreign room
    app._voice_rooms = {}
    app.on_voice_state("v" * 32, [{"viewer": "A" * 64, "dest": "a" * 32,
                                   "muted": False}])
    pump()
    check("push stored roster", len(app._voice_rooms.get("v" * 32, [])) == 1)
    check("row refreshed by push", "1 in call" in row._label.text,
          row._label.text)

    # joined routing: the room roster reaches the engine
    eng = FakeEngine()
    app._voice_engine = eng
    app._voice_room_cid = "v" * 32
    app.on_voice_state("v" * 32, [
        {"viewer": "A" * 64, "dest": "a" * 32, "muted": False},
        {"viewer": "B" * 64, "dest": "b" * 32, "muted": False}])
    pump()
    check("engine got the joined room's roster",
          eng.rosters and len(eng.rosters[-1][1]) == 2,
          eng.rosters[-1] if eng.rosters else "none")

    # the k:"voice" raw push frame reaches packet_relayed
    from RNS.vendor import umsgpack as msgpack
    frame = msgpack.packb({"k": "voice", "dest": "b" * 32, "body": b"PK1"})
    app._on_push_frame(frame)
    pump()
    check("raw voice push fed the engine",
          eng.relayed and eng.relayed[0] == ("b" * 32, b"PK1"), eng.relayed)

    # guild_api.voice_publish encodes the raw packet
    import base64
    import guild_api as ga
    sent = {}

    class T(ga.GuildAPI):
        def __init__(self):
            pass

        def _request_json(self, path, data, timeout=10.0):
            sent["path"], sent["data"] = path, data
            return {"ok": 1}

    T().voice_publish("c" * 32, b"\x00\x05codecpkt")
    ok = sent["path"] == "/api/v1/voice/publish" and \
        sent["data"]["body"] == base64.b64encode(b"\x00\x05codecpkt").decode()
    check("voice_publish b64 payload", ok, sent.get("data"))

    # tay 2026-11-25: member lines go green while transmitting (dest in
    # the speaking set; the you-row green while this client's tx is
    # live), and dim again when nobody transmits
    row = app._voice_row_for("voicerow-lane", "v" * 32)
    members = [
        {"viewer": app.my_user_hash, "dest": "0" * 32, "muted": False},
        {"viewer": "abcd1234" + "0" * 56, "dest": "f" * 32, "muted": False},
    ]
    row.render(members, joined=True,
               speaking=["f" * 32], tx_mode="always",
               self_speaking=True, my_viewer=app.my_user_hash)
    ok = row._member_lines[0].color == row.GREEN \
        and row._member_lines[0].text.find("you") >= 0
    ok2 = row._member_lines[1].color == row.GREEN \
        and row._member_lines[1].text.find("speaking") >= 0
    check("member lines go green while transmitting", ok and ok2)
    row.render(members, joined=True, speaking=[], tx_mode="always",
               self_speaking=False, my_viewer=app.my_user_hash)
    ok = row._member_lines[0].color == row.DIM \
        and row._member_lines[1].color == row.DIM
    check("member lines dim again when nobody transmits", ok)

    # the tx-mode pick routes the panel tap into the real engine
    import app_voice as av
    eng3 = av.VoiceEngine()
    app._voice_engine = eng3
    app._voice_tap_mode("voicerow-lane", "vad")
    app._voice_tap_ptt("voicerow-lane", True)
    check("tx-mode pick reaches the engine",
          eng3.voice_mode == "vad" and eng3._ptt_down)

    # engine teardown is clean in-app: leave clears the room
    app._voice_room_cid = None
    eng.leave_r = True

    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
