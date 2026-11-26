#!/usr/bin/env python3
"""Polish (h): image embeds -- shimmer skeleton placeholder + reveal
fade-in.

Asserts STATE, never wall-clock timing or frame counts. The 0.42s
reveal animation is driven deterministically: the running Kivy
Animation is advanced by explicit _update(dt) calls, so no real time
has to elapse. The fetch thread is deferred (never started) until the
test releases it, so the placeholder is observable mid-flight.

- _add_loading_placeholder: image kinds get a SkeletonRow (centred in
  a 200px box, the exact height of the embed that replaces it, so the
  row does not jump); audio/video/file kinds keep the text Label
- ui_fx.SkeletonRow: shimmers while motion is on; with REDUCED_MOTION
  on it renders statically (no running phase animation, phase stays
  0); flipping the flag stops / restarts live rows
- _add_attachment_embed (image): MessageEmbed starts at opacity 0,
  passes through a strictly partial opacity mid-fade, lands at 1;
  dispatching on_release (the event a tap produces) still fires
  show_fullscreen_image, and the status-line binding still reports
  the file. (A real touch never reaches ButtonBehavior in this tree:
  MessageEmbed(AsyncImage, ButtonBehavior) puts Widget.on_touch_down
  ahead of ButtonBehavior's in the MRO -- a pre-existing defect,
  verified against the baseline; the dispatch check pins the handler
  wiring this group must not regress.)
- REDUCED_MOTION on: the embed lands at opacity 1 at once
- _fetch_attachment end to end: skeleton placeholder while the bytes
  are in flight, embed after release, placeholder gone; a failed
  fetch removes the placeholder and renders nothing; an audio fetch
  keeps the text placeholder and renders an audio row
- _add_site_media: the embed starts as an empty box, then lands its
  source with the same reveal (opacity 0 -> 1); reduced motion lands
  it at opacity 1 at once

Harness modelled on tests/test_polish_f_download_row.py (StubApp)
and tests/test_polish_b_message_entrance.py (animation pumping).
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
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402

PASS, FAIL = [], []

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")

PATH = "/file/attachments/abc.png"


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


def _anim_for(widget, prop):
    for anim in list(Animation._instances):
        entry = getattr(anim, "_widgets", {}).get(widget.uid)
        if entry is not None and prop in entry.get("properties", {}):
            return anim
    return None


def _step(widget, prop, dt):
    anim = _anim_for(widget, prop)
    if anim is None:
        return False
    anim._update(dt)
    return True


def _pump_to_end(widget, prop):
    for _ in range(10):
        if _anim_for(widget, prop) is None:
            break
        _step(widget, prop, 0.15)
    return widget.opacity


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


class FakeRow(object):
    def __init__(self, msg_id):
        from kivy.uix.boxlayout import BoxLayout
        self.msg_id = msg_id
        self.ids = types.SimpleNamespace(
            embed_container=BoxLayout(orientation="vertical"))
        self._loading_placeholder = None


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
        self._attach_dir = tempfile.mkdtemp(prefix="shades-imgemb-test-")
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


def _skeleton_of(placeholder):
    """The SkeletonRow inside an image placeholder box, or None."""
    if not isinstance(placeholder, ui_fx.SkeletonRow):
        kids = getattr(placeholder, "children", [])
        for kid in kids:
            if isinstance(kid, ui_fx.SkeletonRow):
                return kid
    return placeholder if isinstance(placeholder,
                                     ui_fx.SkeletonRow) else None


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.lang import Builder
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.anchorlayout import AnchorLayout
    from kivy.metrics import dp
    from app_site import MessageEmbed
    from client_widgets import _AudioAttachRow

    Builder.load_file("kv_md.kv")

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    running = _ThemeApp()

    # The fetch paths run their worker on a deferred thread and their
    # UI callbacks on an inline clock, so the whole flow is
    # deterministic: placeholder first, embed only after release.
    real_att_clock, real_att_thread = (app_attachments.Clock,
                                       app_attachments.threading)
    real_site_clock, real_site_thread = (app_site.Clock,
                                         app_site.threading)
    app_attachments.Clock = _NowClock
    app_attachments.threading = types.SimpleNamespace(Thread=_DeferredThread)
    app_site.Clock = _NowClock
    app_site.threading = types.SimpleNamespace(Thread=_DeferredThread)
    try:
        app = StubApp(kc, files={PATH: PNG_1PX})

        # --- 1. image placeholder is a shimmer skeleton ---------------
        for name in ("shot.png", "photo.jpg", "pic.jpeg", "anim.gif",
                     "img.webp", "pic.bmp", "PHOTO.PNG"):
            row = FakeRow("m-ph-%s" % name)
            app._add_loading_placeholder(row, name)
            ph = row._loading_placeholder
            check("image %s placeholder is a 200px box" % name,
                  isinstance(ph, AnchorLayout) and ph.height == dp(200),
                  "%s h=%r" % (type(ph).__name__, getattr(ph, "height", None)))
            skel = _skeleton_of(ph)
            check("image %s placeholder holds a SkeletonRow" % name,
                  skel is not None and len(ph.children) == 1,
                  repr([type(c).__name__ for c in ph.children]))
            check("image %s skeleton is 70px tall" % name,
                  skel is not None and skel.height == dp(70),
                  repr(None if skel is None else skel.height))
            check("image %s skeleton fills the box width" % name,
                  skel is not None and skel.size_hint_x == 1,
                  repr(None if skel is None else skel.size_hint_x))
            check("image %s placeholder is flagged" % name,
                  getattr(ph, "_is_loading_placeholder", False) is True)
            check("image %s placeholder is in the container" % name,
                  ph in row.ids.embed_container.children)
            app._remove_placeholder(row)
            check("image %s placeholder removes cleanly" % name,
                  row._loading_placeholder is None
                  and row.ids.embed_container.children == [])

        # --- 2. audio/video/file keep the text label ------------------
        cases = [("song.mp3", "[loading audio…]"),
                 ("tune.wav", "[loading audio…]"),
                 ("clip.mp4", "[loading video…]"),
                 ("movie.webm", "[loading video…]"),
                 ("notes.txt", "[loading file…]"),
                 ("doc.pdf", "[loading file…]"),
                 ("", "[loading file…]")]
        for name, want in cases:
            row = FakeRow("m-tx-%s" % (name or "empty"))
            app._add_loading_placeholder(row, name)
            ph = row._loading_placeholder
            check("%s placeholder is the text label" % (name or "(empty)"),
                  isinstance(ph, Label) and ph.text == want,
                  "%s text=%r" % (type(ph).__name__,
                                  getattr(ph, "text", None)))
            check("%s label is 200px tall" % (name or "(empty)"),
                  isinstance(ph, Label) and ph.height == dp(200))
            check("%s label holds no SkeletonRow" % (name or "(empty)"),
                  _skeleton_of(ph) is None)
            app._remove_placeholder(row)
            check("%s label removes cleanly" % (name or "(empty)"),
                  row._loading_placeholder is None
                  and row.ids.embed_container.children == [])

        # --- 3. SkeletonRow honours REDUCED_MOTION --------------------
        ui_fx.set_reduced_motion(False)
        live = ui_fx.SkeletonRow()
        check("skeleton shimmers while motion is on",
              ui_fx.is_animating(live, "phase"),
              "anim=%r" % (live._anim,))
        ui_fx.set_reduced_motion(True)
        check("flipping the flag stops a live skeleton",
              not ui_fx.is_animating(live, "phase")
              and live._anim is None)
        ui_fx.set_reduced_motion(False)
        check("flipping the flag back restarts it",
              ui_fx.is_animating(live, "phase"))
        ui_fx.set_reduced_motion(True)
        try:
            still = ui_fx.SkeletonRow()
            check("skeleton built under reduced motion starts no "
                  "animation", not ui_fx.is_animating(still, "phase")
                  and still._anim is None)
            check("reduced-motion skeleton phase starts at 0",
                  still.phase == 0, repr(still.phase))
            for _ in range(3):
                Clock.tick()
            check("reduced-motion skeleton phase stays 0 after ticks",
                  still.phase == 0, repr(still.phase))
            # The image placeholder built under reduced motion is a
            # static skeleton, not a shimmering one.
            row = FakeRow("m-reduced-ph")
            app._add_loading_placeholder(row, "shot.png")
            rskel = _skeleton_of(row._loading_placeholder)
            check("reduced-motion image placeholder is a static "
                  "skeleton",
                  rskel is not None
                  and not ui_fx.is_animating(rskel, "phase"),
                  repr(None if rskel is None else rskel._anim))
            app._remove_placeholder(row)
        finally:
            ui_fx.set_reduced_motion(False)

        # --- 4. image embed fades in ----------------------------------
        row = FakeRow("m-emb-1")
        app._add_attachment_embed(row, "shot.png", PNG_1PX)
        embeds = row.ids.embed_container.children
        check("image embed renders exactly one widget",
              len(embeds) == 1, "%d" % len(embeds))
        emb = embeds[0] if embeds else None
        check("image embed is a MessageEmbed",
              isinstance(emb, MessageEmbed),
              repr(None if emb is None else type(emb).__name__))
        check("image embed starts at opacity 0",
              emb is not None and emb.opacity == 0, repr(
                  None if emb is None else emb.opacity))
        check("image embed has a running opacity animation",
              emb is not None
              and _anim_for(emb, "opacity") is not None)
        _step(emb, "opacity", 0.21)  # first update arms; opacity still 0
        check("image embed still 0 after the arming update",
              emb.opacity == 0, repr(emb.opacity))
        _step(emb, "opacity", 0.21)  # halfway through the 0.42s fade
        check("image embed is mid-fade (0 < opacity < 1)",
              0 < emb.opacity < 1, repr(emb.opacity))
        check("image embed animation still running at mid-fade",
              _anim_for(emb, "opacity") is not None)
        check("image embed lands at opacity 1",
              _pump_to_end(emb, "opacity") == 1, repr(emb.opacity))
        check("image embed animation finished after landing",
              _anim_for(emb, "opacity") is None)

        # --- 5. on_release still fires fullscreen + status ------------
        # A real touch never reaches ButtonBehavior here (see the
        # docstring: pre-existing MRO defect), so the check dispatches
        # the on_release event itself -- the event a working tap
        # produces -- which must reach both the class handler
        # (show_fullscreen_image) and the status-line binding.
        seen = {}

        def recorder(source, embed=None, **k):
            seen["source"] = source
            seen["embed"] = embed

        running.show_fullscreen_image = recorder
        try:
            n_status = len(app.status)
            emb.dispatch("on_release")
        finally:
            del running.show_fullscreen_image
        check("on_release fires show_fullscreen_image",
              seen.get("source") == emb.source,
              repr(seen.get("source")))
        check("on_release passes the embed itself",
              seen.get("embed") is emb)
        check("on_release still reports the file in the status line",
              len(app.status) == n_status + 1
              and app.status[-1] == "shot.png (0 KB)",
              repr(app.status[n_status:]))

        # --- 6. reduced motion: embed lands at once --------------------
        ui_fx.set_reduced_motion(True)
        try:
            row2 = FakeRow("m-emb-reduced")
            app._add_attachment_embed(row2, "shot.png", PNG_1PX)
            emb2 = row2.ids.embed_container.children[0]
            check("reduced-motion embed is opacity 1 at once",
                  emb2.opacity == 1, repr(emb2.opacity))
            check("reduced-motion embed starts no animation",
                  _anim_for(emb2, "opacity") is None)
        finally:
            ui_fx.set_reduced_motion(False)

        # --- 7. fetch flow: skeleton, then embed -----------------------
        # clear the blob cache first: the site-embed lanes above saved
        # shot.png, and the resume-race cache-first restore would render
        # it with zero network (that contract has its own battery);
        # recreate the dir since this stub's _attachments_dir does not
        # makedirs the way production does
        import shutil
        shutil.rmtree(app._attach_dir, ignore_errors=True)
        os.makedirs(app._attach_dir, exist_ok=True)
        row3 = FakeRow("m-fetch-img")
        app._msg_widgets["m-fetch-img"] = row3
        app._fetch_attachment(
            row3, "m-fetch-img",
            {"name": "shot.png", "path": PATH, "size": len(PNG_1PX)})
        check("fetch shows the skeleton placeholder mid-flight",
              _skeleton_of(row3._loading_placeholder) is not None
              and row3._loading_placeholder in
              row3.ids.embed_container.children,
              repr(type(row3._loading_placeholder).__name__))
        check("fetch renders no embed before the bytes arrive",
              row3.ids.embed_container.children == [
                  row3._loading_placeholder],
              "%d children" % len(row3.ids.embed_container.children))
        _release_threads()
        check("fetch removes the placeholder once landed",
              row3._loading_placeholder is None)
        landed = row3.ids.embed_container.children
        check("fetch renders exactly one embed after landing",
              len(landed) == 1, "%d" % len(landed))
        emb3 = landed[0] if landed else None
        check("landed embed is a MessageEmbed",
              isinstance(emb3, MessageEmbed))
        check("landed embed starts at opacity 0",
              emb3 is not None and emb3.opacity == 0,
              repr(None if emb3 is None else emb3.opacity))
        check("landed embed reaches opacity 1",
              emb3 is not None
              and _pump_to_end(emb3, "opacity") == 1,
              repr(None if emb3 is None else emb3.opacity))

        # --- 8. fetch flow: audio keeps the label, renders audio ------
        row4 = FakeRow("m-fetch-aud")
        app._msg_widgets["m-fetch-aud"] = row4
        app.guild_api.files["/file/attachments/song.mp3"] = b"ID3" + b"\0" * 64
        app._fetch_attachment(
            row4, "m-fetch-aud",
            {"name": "song.mp3", "path": "/file/attachments/song.mp3",
             "size": 67})
        ph4 = row4._loading_placeholder
        check("audio fetch placeholder is the audio label",
              isinstance(ph4, Label) and ph4.text == "[loading audio…]",
              repr(getattr(ph4, "text", None)))
        _release_threads()
        check("audio fetch removes the label once landed",
              row4._loading_placeholder is None)
        aud = row4.ids.embed_container.children
        check("audio fetch renders an audio row",
              len(aud) == 1 and isinstance(aud[0], _AudioAttachRow),
              repr([type(c).__name__ for c in aud]))

        # --- 9. fetch flow: failure clears the placeholder -------------
        row5 = FakeRow("m-fetch-fail")
        app._msg_widgets["m-fetch-fail"] = row5
        app._fetch_attachment(
            row5, "m-fetch-fail",
            {"name": "gone.png", "path": "/file/attachments/gone.png",
             "size": 10})
        check("failed fetch shows the skeleton while in flight",
              _skeleton_of(row5._loading_placeholder) is not None)
        _release_threads()
        # resume-race contract: the first miss with the link not ready
        # RETRIES (placeholder up, no terminal error yet); the second miss
        # is the capped terminal named error
        check("first missed fetch retries, placeholder stays up",
              row5._loading_placeholder is not None
              and row5.ids.embed_container.children
              == [row5._loading_placeholder]
              and any(s.endswith("link not ready, retrying")
                      for s in app.status)
              and not any("attachment fetch failed" in s
                          for s in app.status),
              repr(app.status[-2:]))
        # drain the retry chain this instant-clock harness spawns: under
        # _NowClock each Clock schedule fires inline, so one release runs
        # a retry body that queues the next work; the capped attempt
        # lands the terminal named error and clears the placeholder
        for _ in range(6):
            _release_threads()
            if (row5._loading_placeholder is None
                    and any(s == "attachment fetch failed: gone.png"
                            for s in app.status)):
                break
        check("failed fetch removes the placeholder",
              row5._loading_placeholder is None
              and row5.ids.embed_container.children == [])
        check("failed fetch renders no embed",
              row5.ids.embed_container.children == [])
        check("failed fetch reports in the status line",
              any(s == "attachment fetch failed: gone.png"
                  for s in app.status),
              repr(app.status[-2:]))

        # --- 10. site media: same reveal on landing --------------------
        site = types.SimpleNamespace(
            guild_api=StubGuild({PATH: PNG_1PX}),
            _save_attachment_blob=app._save_attachment_blob,
            _set_status=lambda text: None)
        site._add_site_media = types.MethodType(
            app_site.SiteMixin._add_site_media, site)
        container = BoxLayout(orientation="vertical")
        site._add_site_media(container, "c" * 64,
                             {"link": PATH})
        check("site media renders one embed box at once",
              len(container.children) == 1
              and isinstance(container.children[0], MessageEmbed),
              repr([type(c).__name__ for c in container.children]))
        semb = container.children[0]
        check("site embed starts with an empty source",
              semb.source == "", repr(semb.source))
        check("site embed starts at opacity 1 (no reveal yet)",
              semb.opacity == 1, repr(semb.opacity))
        _release_threads()
        check("site embed lands its source",
              semb.source.endswith("abc.png")
              and os.path.exists(semb.source),
              repr(semb.source))
        check("site embed starts at opacity 0 on landing",
              semb.opacity == 0, repr(semb.opacity))
        check("site embed has a running opacity animation",
              _anim_for(semb, "opacity") is not None)
        check("site embed lands at opacity 1",
              _pump_to_end(semb, "opacity") == 1, repr(semb.opacity))

        # --- 11. site media: reduced motion lands at once -------------
        ui_fx.set_reduced_motion(True)
        try:
            container2 = BoxLayout(orientation="vertical")
            site._add_site_media(container2, "c" * 64,
                                 {"link": PATH})
            semb2 = container2.children[0]
            _release_threads()
            check("reduced-motion site embed lands at opacity 1",
                  semb2.opacity == 1, repr(semb2.opacity))
            check("reduced-motion site embed starts no animation",
                  _anim_for(semb2, "opacity") is None)
        finally:
            ui_fx.set_reduced_motion(False)
    finally:
        app_attachments.Clock = real_att_clock
        app_attachments.threading = real_att_thread
        app_site.Clock = real_site_clock
        app_site.threading = real_site_thread
        ui_fx.set_reduced_motion(False)

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
