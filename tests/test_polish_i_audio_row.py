#!/usr/bin/env python3
"""Polish (i): audio-row polish -- the three plain grey Buttons of
_AudioAttachRow become FancyMDButtons (play primary, stop/save
secondary), with the text living in an MDButtonText child
(``_fancy_label``) so the play/pause flip targets the label.

Asserts STATE, never wall-clock timing: the buttons are FancyMDButton
with the expected variants and 11sp labels; the row stays 40px with
the same widths; play toggles via a stubbed player and the label
child flips play<->pause; stop resets playing/paused and the label
to "play"; save reports the status line via a stubbed _set_status;
a no-player row does nothing on play/stop/save and raises nothing;
the volume Slider is untouched (still a plain Kivy Slider -- the
remaining observation, recorded not attempted); the fetch flow still
renders an audio row with themed buttons; all strings ASCII.

Harness modelled on tests/test_polish_f_download_row.py (direct row)
and tests/test_polish_h_image_embed.py (fetch flow with the deferred
thread + inline clock).
"""
import functools
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_attachments  # noqa: E402
import app_site  # noqa: E402
from kivy.metrics import dp, sp  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.slider import Slider  # noqa: E402
from kivymd.uix.button import MDButtonText  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _NowClock(object):
    @staticmethod
    def schedule_once(fn, dt=0):
        fn(0)


class _DeferredThread(object):
    """A thread that never runs until the test releases it."""
    pending = []

    def __init__(self, target=None, daemon=False, **k):
        self._target = target

    def start(self):
        _DeferredThread.pending.append(self._target)


def _release_threads():
    pending, _DeferredThread.pending = _DeferredThread.pending, []
    for target in pending:
        target()


class StubMedia(object):
    """Pausable stand-in for the app's _audio_media."""

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def release(self):
        self.calls.append("release")


class NoPauseMedia(object):
    """Media with no pause: the row must stop-then-restart it."""

    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")


class StubPlayer(object):
    """Minimal stand-in for sHadesClientApp's audio side."""

    def __init__(self, media=None):
        self._audio_media = None
        self._media = media if media is not None else StubMedia()
        self.status = []
        self.played = []

    def _play_audio(self, path):
        self.played.append(path)
        self._audio_media = self._media
        return self._media  # non-None sentinel: playback started

    def _set_status(self, text):
        self.status.append(text)


class FakeRow(object):
    def __init__(self, msg_id):
        self.msg_id = msg_id
        self.ids = types.SimpleNamespace(
            embed_container=BoxLayout(orientation="vertical"))
        self._loading_placeholder = None


class FakeGrid(object):
    def __init__(self):
        self.children = []

    def add_widget(self, w):
        self.children.append(w)


class FakeMain(object):
    def __init__(self):
        self.ids = types.SimpleNamespace(message_grid=FakeGrid())


class FakeRoot(object):
    def __init__(self):
        self._main = FakeMain()

    def get_screen(self, name):
        return self._main


class StubGuild(object):
    def __init__(self, files):
        self.files = files

    def fetch_file(self, path):
        return self.files.get(path)

    def avatar_get(self, user_hash):
        return {"ok": False}


class StubApp(object):
    """Minimal stand-in for sHadesClientApp (no RNS/guild needed)."""

    def __init__(self, kivy_client, files=None):
        self.kc = kivy_client
        self.root = FakeRoot()
        self.guild_api = StubGuild(files or {})
        self.my_hash = "a" * 32
        self.username = "me"
        self._members = {}
        self._msg_widgets = {}
        self.theme_colors = dict(kivy_client.DEFAULT_THEME_COLORS)
        self.status = []
        self._attachment_kind = kivy_client.sHadesClientApp._attachment_kind
        self._attach_dir = tempfile.mkdtemp(prefix="shades-audrow-test-")
        self._attachments_dir = lambda: self._attach_dir
        self._save_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._save_attachment_blob, self)
        self._cached_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._cached_attachment_blob, self)
        self._atts_link_ok = functools.partial(
            kivy_client.sHadesClientApp._atts_link_ok, self)
        self._att_fetch_attempts_pop = functools.partial(
            kivy_client.sHadesClientApp._att_fetch_attempts_pop, self)
        self._add_attachment_embed = functools.partial(
            kivy_client.sHadesClientApp._add_attachment_embed, self)
        self._fetch_attachment = functools.partial(
            kivy_client.sHadesClientApp._fetch_attachment, self)
        self._add_loading_placeholder = functools.partial(
            kivy_client.sHadesClientApp._add_loading_placeholder, self)
        self._loading_placeholder_text = (
            kivy_client.sHadesClientApp._loading_placeholder_text)
        self._remove_placeholder = functools.partial(
            kivy_client.sHadesClientApp._remove_placeholder, self)

    def _set_status(self, text):
        self.status.append(text)

    _status = _set_status


def _label(btn):
    return getattr(btn, "_fancy_label", None)


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.lang import Builder
    from client_widgets import _AudioAttachRow, FancyMDButton

    Builder.load_file("kv_md.kv")

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    _ThemeApp()

    player = StubPlayer()
    parent = BoxLayout(orientation="vertical", size=(400, 200),
                       size_hint=(None, None))
    row = _AudioAttachRow(path="/tmp/song.mp3", name="song.mp3",
                          size=50000, player=player)
    parent.add_widget(row)
    parent.do_layout()

    # --- 1. the three buttons are FancyMDButtons, right variants ------
    check("play button is a FancyMDButton",
          isinstance(row.btn_play, FancyMDButton),
          repr(type(row.btn_play).__name__))
    check("stop button is a FancyMDButton",
          isinstance(row.btn_stop, FancyMDButton),
          repr(type(row.btn_stop).__name__))
    check("save button is a FancyMDButton",
          isinstance(row.btn_save, FancyMDButton),
          repr(type(row.btn_save).__name__))
    check("play button is the primary variant",
          row.btn_play.variant == "primary", repr(row.btn_play.variant))
    check("stop button is the secondary variant",
          row.btn_stop.variant == "secondary", repr(row.btn_stop.variant))
    check("save button is the secondary variant",
          row.btn_save.variant == "secondary", repr(row.btn_save.variant))
    check("no plain Kivy Button remains in the row",
          not any(type(c).__name__ == "Button" for c in row.children),
          repr([type(c).__name__ for c in row.children]))

    # --- 2. labels live in MDButtonText children, 11sp ----------------
    for btn, text in ((row.btn_play, "play"), (row.btn_stop, "stop"),
                      (row.btn_save, "save")):
        label = _label(btn)
        check("%s label is an MDButtonText child" % text,
              isinstance(label, MDButtonText),
              repr(None if label is None else type(label).__name__))
        check("%s label reads %r" % (text, text),
              label is not None and label.text == text,
              repr(None if label is None else label.text))
        check("%s label is 11sp" % text,
              label is not None and label.font_size == sp(11),
              repr(None if label is None else label.font_size))
        check("%s label has an opaque 4-component colour" % text,
              label is not None and len(label.text_color) == 4
              and label.text_color[3] == 1,
              repr(None if label is None else label.text_color))
    check("play label is dark on the amber primary",
          _label(row.btn_play).text_color[0] < 0.3
          and _label(row.btn_play).text_color[1] < 0.3,
          repr(_label(row.btn_play).text_color))
    check("stop/save labels are warm white on secondary",
          _label(row.btn_stop).text_color[0] > 0.8
          and _label(row.btn_save).text_color[0] > 0.8,
          repr((_label(row.btn_stop).text_color,
                _label(row.btn_save).text_color)))

    # --- 3. geometry: 40px row, same widths ---------------------------
    check("audio row stays 40px tall", row.height == dp(40),
          repr(row.height))
    check("play button keeps its 56dp width",
          row.btn_play.width == dp(56), repr(row.btn_play.width))
    check("stop button keeps its 48dp width",
          row.btn_stop.width == dp(48), repr(row.btn_stop.width))
    check("save button keeps its 52dp width",
          row.btn_save.width == dp(52), repr(row.btn_save.width))
    check("audio label still reads '[audio] song.mp3 (48 KB)'",
          row.children[-1].text == "[audio] song.mp3 (48 KB)",
          repr(row.children[-1].text))

    # --- 4. play toggles, label child flips play<->pause --------------
    row._toggle()
    check("first toggle starts playback", row._playing is True
          and player.played == ["/tmp/song.mp3"], repr(player.played))
    check("first toggle flips the label to 'pause'",
          _label(row.btn_play).text == "pause",
          repr(_label(row.btn_play).text))
    check("first toggle leaves the button itself textless",
          getattr(row.btn_play, "text", "") in ("", None),
          repr(getattr(row.btn_play, "text", None)))
    row._toggle()
    check("second toggle pauses the media",
          player._media.calls == ["pause"], repr(player._media.calls))
    check("second toggle flips the label back to 'play'",
          _label(row.btn_play).text == "play",
          repr(_label(row.btn_play).text))
    check("second toggle marks the row paused", row._paused is True)
    # resume branch: paused with _playing False restarts the media
    row._playing = False
    row._toggle()
    check("resume restarts the media",
          player._media.calls == ["pause", "start"],
          repr(player._media.calls))
    check("resume flips the label to 'pause'",
          _label(row.btn_play).text == "pause",
          repr(_label(row.btn_play).text))
    check("resume clears the paused flag",
          row._playing is True and row._paused is False)

    # --- 5. on_release binding still drives the toggle ----------------
    row._stop()
    check("stop after resume resets the label to 'play'",
          _label(row.btn_play).text == "play",
          repr(_label(row.btn_play).text))
    row.btn_play.dispatch("on_release")
    check("dispatching on_release on play starts playback",
          row._playing is True
          and _label(row.btn_play).text == "pause",
          repr(_label(row.btn_play).text))

    # --- 6. stop resets state and releases the media ------------------
    row._stop()
    check("stop clears playing and paused",
          row._playing is False and row._paused is False)
    check("stop flips the label to 'play'",
          _label(row.btn_play).text == "play",
          repr(_label(row.btn_play).text))
    check("stop stops and releases the media",
          player._media.calls[-2:] == ["stop", "release"],
          repr(player._media.calls))
    check("stop clears the player's media",
          player._audio_media is None)

    # --- 7. save reports the status line ------------------------------
    row._report()
    check("save reports the file in the status line",
          player.status == ["audio in app storage: song.mp3"],
          repr(player.status))
    row.btn_save.dispatch("on_release")
    check("dispatching on_release on save re-reports",
          len(player.status) == 2
          and player.status[-1] == "audio in app storage: song.mp3",
          repr(player.status))

    # --- 8. media without pause: toggle stops it ----------------------
    player2 = StubPlayer(media=NoPauseMedia())
    row2 = _AudioAttachRow(path="/tmp/tune.mp3", name="tune.mp3",
                           size=1000, player=player2)
    row2._toggle()
    check("no-pause media starts on first toggle",
          row2._playing is True
          and _label(row2.btn_play).text == "pause")
    row2._toggle()
    check("no-pause media is stopped on second toggle",
          player2._media.calls == ["stop"], repr(player2._media.calls))
    check("no-pause toggle clears playing and the media",
          row2._playing is False and player2._audio_media is None)
    check("no-pause toggle flips the label to 'play'",
          _label(row2.btn_play).text == "play",
          repr(_label(row2.btn_play).text))

    # --- 9. no player: play/stop/save do nothing, raise nothing -------
    row3 = _AudioAttachRow(path="/tmp/orphan.mp3", name="orphan.mp3",
                           size=1000, player=None)
    try:
        row3._toggle()
        row3._stop()
        row3._report()
        row3.btn_play.dispatch("on_release")
        row3.btn_stop.dispatch("on_release")
        row3.btn_save.dispatch("on_release")
        check("no-player row tolerates play/stop/save",
              row3._playing is False and row3._paused is False
              and _label(row3.btn_play).text == "play")
    except Exception as e:
        check("no-player row tolerates play/stop/save", False, repr(e))

    # --- 10. volume slider untouched -----------------------------------
    check("volume control is still a plain Kivy Slider",
          isinstance(row.vol, Slider)
          and type(row.vol).__name__ == "Slider",
          repr(type(row.vol).__name__))
    check("volume slider keeps its 80dp width",
          row.vol.width == dp(80), repr(row.vol.width))

    # --- 11. fetch flow still renders a themed audio row --------------
    real_att_clock, real_att_thread = (app_attachments.Clock,
                                       app_attachments.threading)
    real_site_clock, real_site_thread = (app_site.Clock,
                                         app_site.threading)
    app_attachments.Clock = _NowClock
    app_attachments.threading = types.SimpleNamespace(Thread=_DeferredThread)
    app_site.Clock = _NowClock
    app_site.threading = types.SimpleNamespace(Thread=_DeferredThread)
    try:
        app = StubApp(kc, files={"/file/attachments/song.mp3":
                                 b"ID3" + b"\0" * 64})
        row4 = FakeRow("m-fetch-aud")
        app._msg_widgets["m-fetch-aud"] = row4
        app._fetch_attachment(
            row4, "m-fetch-aud",
            {"name": "song.mp3", "path": "/file/attachments/song.mp3",
             "size": 67})
        _release_threads()
        aud = row4.ids.embed_container.children
        check("audio fetch renders an audio row",
              len(aud) == 1 and isinstance(aud[0], _AudioAttachRow),
              repr([type(c).__name__ for c in aud]))
        if len(aud) == 1 and isinstance(aud[0], _AudioAttachRow):
            fetched = aud[0]
            check("fetched audio row play is primary FancyMDButton",
                  isinstance(fetched.btn_play, FancyMDButton)
                  and fetched.btn_play.variant == "primary",
                  repr((type(fetched.btn_play).__name__,
                        fetched.btn_play.variant)))
            check("fetched audio row stop/save are secondary",
                  fetched.btn_stop.variant == "secondary"
                  and fetched.btn_save.variant == "secondary",
                  repr((fetched.btn_stop.variant,
                        fetched.btn_save.variant)))
            check("fetched audio row is 40px tall",
                  fetched.height == dp(40), repr(fetched.height))
    finally:
        app_attachments.Clock, app_attachments.threading = (
            real_att_clock, real_att_thread)
        app_site.Clock, app_site.threading = (
            real_site_clock, real_site_thread)

    # --- 12. every new UI string is ASCII ------------------------------
    strings = [_label(row.btn_play).text, _label(row.btn_stop).text,
               _label(row.btn_save).text, row.children[-1].text,
               player.status[0]]
    check("all audio-row strings are ASCII",
          all(all(ord(c) < 128 for c in s) for s in strings),
          repr(strings))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
