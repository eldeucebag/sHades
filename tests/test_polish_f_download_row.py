#!/usr/bin/env python3
"""Polish (f): DownloadRow -- a real download-row widget for video /
generic-file attachments, replacing the bare 20px tap-to-save label.

Asserts STATE, never wall-clock timing: the row renders with name +
size + unsaved state at >= 40px height; a tap moves it to saving
then saved (the Clock is driven explicitly); the saved state persists
across a second tap; a failed write shows the failed state; the row's
tap target covers the full row width; no exceptions. Deterministic
under xvfb-run. Harness modelled on tests/test_attachments.py
(StubApp) and tests/test_channel_list.py (FakeTouch).
"""
import functools
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class FakeTouch:
    """Just enough of a Kivy touch for ButtonBehavior's down/up chain:
    grab() stamps grab_current (on_touch_up asserts the grab), ungrab()
    clears it."""

    def __init__(self, x, y):
        self.x, self.y = x, y
        self.ud = {}
        self.grab_current = None
        self.profile = []
        self.button = "left"
        self.is_mouse_scrolling = False

    @property
    def pos(self):
        return (self.x, self.y)

    def grab(self, widget, *a, **k):
        self.grab_current = widget

    def ungrab(self, widget, *a, **k):
        if self.grab_current is widget:
            self.grab_current = None


def _tap(row, x, y):
    """A full press/release cycle at (x, y) on the row, as a user tap."""
    touch = FakeTouch(x, y)
    row.on_touch_down(touch)
    row.on_touch_up(touch)


def _tick(n=3):
    from kivy.clock import Clock
    for _ in range(n):
        Clock.tick()


class FakeIds:
    def __init__(self, grid):
        self.message_grid = grid
        self.msg_input = FakeInput()


class FakeInput:
    text = ""


class FakeMain:
    _current_channel = "general"

    def __init__(self, grid):
        self.ids = FakeIds(grid)


class FakeRoot:
    def __init__(self, grid):
        self._main = FakeMain(grid)

    def get_screen(self, name):
        return self._main


class StubApp:
    """Minimal stand-in for sHadesClientApp (no RNS/guild needed).
    Copied from tests/test_attachments.py: the same real methods bound
    to this stub, saves routed into a temp dir."""

    def __init__(self, kivy_client, grid):
        self.kc = kivy_client
        self.root = FakeRoot(grid)
        self.guild_api = None
        self.my_hash = "a" * 32
        self.username = "me"
        self._members = {}
        self._msg_widgets = {}
        self._pending_reply = None
        self._pending_attachment = None
        self._channel_kinds = {}
        self.theme_colors = dict(kivy_client.DEFAULT_THEME_COLORS)
        self.status = []
        self._render_message = functools.partial(
            kivy_client.sHadesClientApp._render_message, self)
        self._add_attachment_embed = functools.partial(
            kivy_client.sHadesClientApp._add_attachment_embed, self)
        self._attachment_kind = kivy_client.sHadesClientApp._attachment_kind
        self._attach_dir = tempfile.mkdtemp(prefix="shades-dlrow-test-")
        self._attachments_dir = lambda: self._attach_dir
        self._save_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._save_attachment_blob, self)
        self._cached_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._cached_attachment_blob, self)
        self._atts_link_ok = functools.partial(
            kivy_client.sHadesClientApp._atts_link_ok, self)
        self._att_fetch_attempts_pop = functools.partial(
            kivy_client.sHadesClientApp._att_fetch_attempts_pop, self)
        self._download_tap = functools.partial(
            kivy_client.sHadesClientApp._download_tap, self)
        self._save_and_report = functools.partial(
            kivy_client.sHadesClientApp._save_and_report, self)
        self._record_member = functools.partial(
            kivy_client.sHadesClientApp._record_member, self)
        self._refresh_avatar = functools.partial(
            kivy_client.sHadesClientApp._refresh_avatar, self)
        self._add_loading_placeholder = functools.partial(
            kivy_client.sHadesClientApp._add_loading_placeholder, self)
        self._loading_placeholder_text = (
            kivy_client.sHadesClientApp._loading_placeholder_text)
        self._remove_placeholder = functools.partial(
            kivy_client.sHadesClientApp._remove_placeholder, self)
        self._set_row_reactions = functools.partial(
            kivy_client.sHadesClientApp._set_row_reactions, self)
        self._channel_kind = functools.partial(
            kivy_client.sHadesClientApp._channel_kind, self)

    def _set_status(self, text):
        self.status.append(text)

    def _channel_cid(self, name):
        return "cid-" + name


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock  # noqa: F401
    from kivy.lang import Builder
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.gridlayout import GridLayout
    from client_widgets import DownloadRow

    Builder.load_file("kv_md.kv")

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    _ThemeApp()

    grid = GridLayout(cols=1)
    app = StubApp(kc, grid)
    blob = b"V" * 12345

    # --- 1. direct row: video renders with name + size + unsaved ------
    parent = BoxLayout(orientation="vertical", size=(400, 200),
                       size_hint=(None, None))
    row = DownloadRow(kind="video", name="clip.mp4", size=len(blob),
                      blob=blob, player=app)
    parent.add_widget(row)
    parent.do_layout()
    check("download row is at least 40px tall", row.height >= 40,
          repr(row.height))
    check("download row starts unsaved", row.dl_state == "unsaved",
          repr(row.dl_state))
    check("unsaved state label reads 'tap to save'",
          row.state_label.text == "tap to save",
          repr(row.state_label.text))
    check("video kind tag reads '[video]'",
          row.kind_label.text == "[video]", repr(row.kind_label.text))
    check("name label shows the file name",
          row.name_label.text == "clip.mp4", repr(row.name_label.text))
    check("size label shows KB", row.size_label.text == "12 KB",
          repr(row.size_label.text))
    check("row fills its parent width", abs(row.width - 400) < 1,
          repr(row.width))
    check("file kind tag reads '[file]'",
          DownloadRow(kind="file", name="n.txt", size=100, blob=b"x",
                      player=app).kind_label.text == "[file]")

    # --- 2. tap: saving, then saved (Clock driven explicitly) ---------
    _tap(row, row.center_x, row.center_y)
    check("tap moves the row to saving before the next tick",
          row.dl_state == "saving", repr(row.dl_state))
    check("saving state label reads 'saving...'",
          row.state_label.text == "saving...",
          repr(row.state_label.text))
    _tick()
    check("save lands the row in saved", row.dl_state == "saved",
          repr(row.dl_state))
    check("saved state label reads 'saved'",
          row.state_label.text == "saved", repr(row.state_label.text))
    check("save reports the location in the status line",
          any(s.startswith("saved clip.mp4 to app storage (12 KB)")
              for s in app.status),
          repr(app.status))
    saved_files = [f for f in os.listdir(app._attach_dir)
                   if f.endswith("clip.mp4")]
    check("save wrote the blob to app storage",
          len(saved_files) == 1
          and open(os.path.join(app._attach_dir, saved_files[0]),
                   "rb").read() == blob,
          repr(saved_files))

    # --- 3. second tap: saved persists, re-reports --------------------
    n_status = len(app.status)
    _tap(row, row.center_x, row.center_y)
    check("second tap passes through saving",
          row.dl_state == "saving", repr(row.dl_state))
    _tick()
    check("saved state persists across a second tap",
          row.dl_state == "saved", repr(row.dl_state))
    check("second tap re-reports the save",
          len(app.status) == n_status + 1
          and app.status[-1].startswith("saved clip.mp4"),
          repr(app.status[-1:]))

    # --- 4. failed write shows the failed state ------------------------
    app2 = StubApp(kc, GridLayout(cols=1))
    app2._save_attachment_blob = lambda blob, name: None
    row2 = DownloadRow(kind="file", name="notes.txt", size=5, blob=b"hello",
                       player=app2)
    _tap(row2, row2.center_x, row2.center_y)
    _tick()
    check("failed write lands the row in failed",
          row2.dl_state == "failed", repr(row2.dl_state))
    check("failed state label reads 'save failed'",
          row2.state_label.text == "save failed",
          repr(row2.state_label.text))
    check("failed write reports 'save failed' in the status line",
          app2.status == ["save failed for notes.txt"],
          repr(app2.status))

    # --- 5. no player: tap fails cleanly, no exception -----------------
    row3 = DownloadRow(kind="file", name="orphan.bin", size=1, blob=b"z",
                       player=None)
    try:
        _tap(row3, row3.center_x, row3.center_y)
        _tick()
        check("row with no player lands in failed, no raise",
              row3.dl_state == "failed", repr(row3.dl_state))
    except Exception as e:
        check("row with no player lands in failed, no raise", False,
              repr(e))

    # --- 6. tap target covers the full row width -----------------------
    parent2 = BoxLayout(orientation="vertical", size=(400, 200),
                        size_hint=(None, None))
    row4 = DownloadRow(kind="video", name="wide.mp4", size=2048,
                       blob=b"W" * 2048, player=app)
    parent2.add_widget(row4)
    parent2.do_layout()
    _tap(row4, row4.x + 2, row4.center_y)  # far left edge
    check("tap at the far left edge starts the save",
          row4.dl_state == "saving", repr(row4.dl_state))
    _tick()
    check("left-edge tap lands saved", row4.dl_state == "saved",
          repr(row4.dl_state))
    parent3 = BoxLayout(orientation="vertical", size=(400, 200),
                        size_hint=(None, None))
    row5 = DownloadRow(kind="file", name="wide.txt", size=2048,
                       blob=b"W" * 2048, player=app)
    parent3.add_widget(row5)
    parent3.do_layout()
    _tap(row5, row5.right - 2, row5.center_y)  # far right edge
    check("tap at the far right edge starts the save",
          row5.dl_state == "saving", repr(row5.dl_state))
    _tick()
    check("right-edge tap lands saved", row5.dl_state == "saved",
          repr(row5.dl_state))
    # a touch outside the row does nothing
    row6 = DownloadRow(kind="file", name="miss.txt", size=10, blob=b"m",
                       player=app)
    parent3.add_widget(row6)
    parent3.do_layout()
    _tap(row6, row6.right + 50, row6.center_y)
    check("touch outside the row does not start a save",
          row6.dl_state == "unsaved", repr(row6.dl_state))

    # --- 7. long names cannot blow the row out -------------------------
    long_name = "a_very_long_file_name_that_keeps_going_and_going_" \
        "and_never_stops.mp4"
    row7 = DownloadRow(kind="video", name=long_name, size=999,
                       blob=b"L" * 999, player=app)
    parent3.add_widget(row7)
    parent3.do_layout()
    check("long name label shortens instead of overflowing",
          row7.name_label.shorten is True, repr(row7.name_label.shorten))
    check("long name keeps the row at 40px and full width",
          row7.height == 40 and abs(row7.width - 400) < 1,
          "h=%r w=%r" % (row7.height, row7.width))

    # --- 8. wiring: real message rows render DownloadRows -------------
    grid8 = GridLayout(cols=1)
    app8 = StubApp(kc, grid8)
    m = {"msg_id": "m-vid", "author_hash": "b" * 32, "username": "bob",
         "content": "watch this", "ts": time.time(),
         "attachments": [["clip.mp4", "video/mp4", b"V" * 5000]]}
    kc.sHadesClientApp._render_message(app8, grid8, m)
    _tick()
    row8 = grid8.children[0]
    embeds = row8.ids.embed_container.children
    check("video attachment renders exactly one embed",
          len(embeds) == 1, "%d embeds" % len(embeds))
    check("video embed is a DownloadRow",
          len(embeds) == 1 and isinstance(embeds[0], DownloadRow),
          repr([type(e).__name__ for e in embeds]))
    check("rendered video row starts unsaved",
          len(embeds) == 1 and embeds[0].dl_state == "unsaved",
          repr([getattr(e, "dl_state", None) for e in embeds]))
    check("rendered video row is 40px tall",
          len(embeds) == 1 and embeds[0].height >= 40,
          repr([getattr(e, "height", None) for e in embeds]))

    grid9 = GridLayout(cols=1)
    app9 = StubApp(kc, grid9)
    m9 = {"msg_id": "m-file", "author_hash": "b" * 32, "username": "bob",
          "content": "the notes", "ts": time.time(),
          "attachments": [["notes.txt", "text/plain", b"hello"]]}
    kc.sHadesClientApp._render_message(app9, grid9, m9)
    _tick()
    embeds9 = grid9.children[0].ids.embed_container.children
    check("generic file attachment renders a DownloadRow",
          len(embeds9) == 1 and isinstance(embeds9[0], DownloadRow)
          and embeds9[0].kind_label.text == "[file]",
          repr([(type(e).__name__, getattr(e, "dl_state", None))
                for e in embeds9]))
    check("rendered file row shows name and size",
          len(embeds9) == 1
          and embeds9[0].name_label.text == "notes.txt"
          and embeds9[0].size_label.text == "0 KB",
          repr([(getattr(e, "name_label", None)
                 and e.name_label.text,
                 getattr(e, "size_label", None)
                 and e.size_label.text) for e in embeds9]))

    # --- 9. every new UI string is ASCII -------------------------------
    strings = [row.state_label.text, row.kind_label.text,
               row.name_label.text, row.size_label.text]
    for state in ("unsaved", "saving", "saved", "failed"):
        probe = DownloadRow(kind="file", name="s", size=0, blob=b"",
                            player=app)
        probe.dl_state = state
        strings.append(probe.state_label.text)
    check("all DownloadRow strings are ASCII",
          all(all(ord(c) < 128 for c in s) for s in strings),
          repr(strings))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
