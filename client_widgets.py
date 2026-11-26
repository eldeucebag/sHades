#!/usr/bin/env python3
"""Client widgets — small widget classes for the sHades Kivy client.

Carved out of kivy_client.py (client split, step 2, stage A). These are
the widget classes that lived between the helpers and sHadesClientApp:
the audio attachment row, the image cache, the context-menu mixin and
overlay, mesh decorations, skeleton rows, and the label/button widgets
built on the mixin (except CtxHeaderLabel, which stays in
kivy_client.py: its menu owns one of the two "copy boot log" items
that tests/test_link_selfheal.py counts in kivy_client.py source).
They need only Kivy + ui_scale (plus os/threading/
hashlib for the cache-path helpers), never the app class, so they stay
import-safe for unit tests.

kivy_client.py keeps a thin re-export so existing names keep working:
kc.ImageCache, kc.ContextMenuMixin, kc.ChannelButton, ... and
kc.IMAGE_CACHE (still instantiated in kivy_client.py). The cache-path
helpers (_app_data_dir, cache_dir, cached_path) moved with ImageCache,
since cached_path is its only caller chain.
"""

import hashlib
import os
import threading
from weakref import WeakSet

from ui_scale import scaled

# ---------------------------------------------------------------------------
# Kivy (guarded, same pattern as kivy_client.py: the module must import
# for linting even when Kivy is absent; stub classes raise on use)
# ---------------------------------------------------------------------------
def _make_kivy_stub(name, err=None):
    class _Stub(object):
        pass

    def _init_stub(*args, **kwargs):
        raise RuntimeError(
            "Kivy stub class %s instantiated; not available, cause=%r"
            % (name, err))

    _Stub.__name__ = name
    _Stub.__init__ = _init_stub
    return _Stub


try:
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
    from kivy.metrics import dp, sp
    from kivy.properties import (
        BooleanProperty, NumericProperty, StringProperty,
    )
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.label import Label
    from kivy.uix.slider import Slider
    from kivy.uix.widget import Widget
    from kivy.utils import get_color_from_hex
    from kivymd.uix.behaviors.hover_behavior import HoverBehavior
    from kivymd.uix.button import MDButton
    import ui_fx
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e

    FloatLayout = _make_kivy_stub("FloatLayout", _e)
    BoxLayout = _make_kivy_stub("BoxLayout", _e)
    Widget = _make_kivy_stub("Widget", _e)
    Label = _make_kivy_stub("Label", _e)
    ButtonBehavior = _make_kivy_stub("ButtonBehavior", _e)
    App = _make_kivy_stub("App", _e)
    MDButton = _make_kivy_stub("MDButton", _e)
    HoverBehavior = _make_kivy_stub("HoverBehavior", _e)
    ui_fx = None

    def _stub_property(*args, **kwargs):
        return None

    StringProperty = NumericProperty = _stub_property
    BooleanProperty = _stub_property


def _fancy_fg(variant):
    """Label colour for a FancyMDButton variant (dark on amber primary,
    white on danger, warm white otherwise); theme first, DESIGN.md
    values as fallbacks. Never raises. Mirrors app_attachments._fancy_fg
    -- duplicated here, not imported, because app_attachments imports
    this module and importing back would cycle."""
    try:
        app = App.get_running_app()
        colors = getattr(app, "theme_colors", None) or {}
    except Exception:
        colors = {}
    if variant == "primary":
        return colors.get("btn_text", (0.06, 0.09, 0.13, 1))
    if variant == "danger":
        return (1, 1, 1, 1)
    return colors.get("text", (0.96, 0.94, 0.91, 1))


def _fancy_button(variant, text, font_size=None, **kw):
    """Build a FancyMDButton with its MDButtonText child (mirrors
    app_attachments._fancy_button; see _fancy_fg for why it lives
    here). The label is kept as ``_fancy_label`` so toggle code can
    re-text it -- MDButton carries no text itself. An explicit
    ``font_size`` also sets ``theme_font_size="Custom"``: MDLabel's
    KV rule drives font_size from the theme font style otherwise,
    and an assigned size is overwritten with the 14sp default."""
    from kivymd.uix.button import MDButtonText
    btn = FancyMDButton(variant=variant, theme_width="Custom", **kw)
    label_kw = {"theme_text_color": "Custom",
                "text_color": _fancy_fg(variant)}
    if font_size is not None:
        label_kw["theme_font_size"] = "Custom"
        label_kw["font_size"] = font_size
    label = MDButtonText(text=text, **label_kw)
    btn.add_widget(label)
    btn._fancy_label = label
    return btn


class _AudioAttachRow(BoxLayout):
    """Embedded audio attachment row (tay, v0.30): play/pause/stop,
    download and volume controls around the sound itself, rendered inside
    the message bubble. Playback delegates to the app's _play_audio():
    Android MediaPlayer when on device, kivy SoundLoader on desktop
    (desktop pause is stop-then-restart; the mixer backend has no pause)."""

    def __init__(self, path="", name="", size=0, player=None, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=dp(40), spacing=dp(6), **kw)
        self._path = path
        self._player = player
        self._playing = False
        self._paused = False
        short = (name or "audio")[:28]
        self.add_widget(Label(
            text="[audio] %s (%d KB)" % (short, size // 1024),
            size_hint_x=1, font_size=sp(12), color=(0.7, 0.73, 0.77, 1)))
        # Polish group 9: FancyMDButtons (play primary, stop/save
        # secondary) instead of plain grey Buttons; the 11sp label
        # size is set on the MDButtonText child, which owns the text.
        self.btn_play = _fancy_button(
            "primary", "play", font_size=sp(11),
            size_hint_x=None, width=dp(56))
        self.btn_play.bind(on_release=self._toggle)
        self.add_widget(self.btn_play)
        self.btn_stop = _fancy_button(
            "secondary", "stop", font_size=sp(11),
            size_hint_x=None, width=dp(48))
        self.btn_stop.bind(on_release=self._stop)
        self.add_widget(self.btn_stop)
        self.btn_save = _fancy_button(
            "secondary", "save", font_size=sp(11),
            size_hint_x=None, width=dp(52))
        self.btn_save.bind(on_release=self._report)
        self.add_widget(self.btn_save)
        self.vol = Slider(min=0, max=1, value=0.8, size_hint_x=None,
                          width=dp(80))
        self.vol.bind(value=self._volume)
        self.add_widget(self.vol)

    def _toggle(self, w=None):
        p = self._player
        if not p:
            return
        mp = getattr(p, "_audio_media", None)
        if self._playing and mp is not None:
            try:
                if hasattr(mp, "pause"):
                    mp.pause()
                    self._paused = True
                else:
                    mp.stop()
                    p._audio_media = None
                    self._playing = False
            except Exception:
                pass
            self.btn_play._fancy_label.text = "play"
        elif self._paused and mp is not None:
            try:
                mp.start()
                self._playing = True
                self._paused = False
            except Exception:
                pass
            self.btn_play._fancy_label.text = "pause"
        else:
            if p._play_audio(self._path) is not None:
                self._playing = True
                self._paused = False
                self.btn_play._fancy_label.text = "pause"

    def _stop(self, w=None):
        p = self._player
        self._playing = False
        self._paused = False
        if p:
            mp = getattr(p, "_audio_media", None)
            if mp is not None:
                try:
                    mp.stop()
                    rel = getattr(mp, "release", None)
                    if rel:
                        rel()
                except Exception:
                    pass
                p._audio_media = None
        self.btn_play._fancy_label.text = "play"

    def _report(self, w=None):
        p = self._player
        if p:
            p._set_status("audio in app storage: %s"
                          % os.path.basename(self._path or "attachment"))

    def _volume(self, w=None, v=0.8):
        p = self._player
        mp = getattr(p, "_audio_media", None) if p else None
        if mp is None:
            return
        try:
            if hasattr(mp, "setVolume"):
                mp.setVolume(float(v), float(v))
            elif hasattr(mp, "volume"):
                mp.volume = float(v)
        except Exception:
            pass


class DownloadRow(ButtonBehavior, BoxLayout):
    """Tappable download row for video / generic-file attachments
    (polish group 6): replaces the bare 20px tap-to-save label. The
    whole row is the tap target (ButtonBehavior on the row itself);
    contents are a kind tag, the (shortened) file name, the size in
    KB, and a state label. States, all ASCII:

    * unsaved -- "tap to save" (text_dim)
    * saving  -- "saving..." (primary amber) while the write is queued
    * saved   -- "saved" (success green); a second tap re-reports
    * failed  -- "save failed" (error red) when the write returns None

    The save is the app's _save_and_report (same status-line wording
    as the old label tap); it runs on the next Clock tick so the
    saving state is visible for a frame. The blob is already in app
    storage from render time -- the tap re-saves idempotently, as
    before. Colours come from theme_colors with DESIGN.md hex
    fallbacks (theme_colors has no success / error key yet, same as
    FancyMDButton's primary_hover / accent_hover / error)."""

    # ButtonBehavior already owns `state` ('normal'/'down'), so the
    # download state is a separate property.
    dl_state = StringProperty("unsaved")

    _STATE_TEXT = {
        "unsaved": "tap to save",
        "saving": "saving...",
        "saved": "saved",
        "failed": "save failed",
    }

    # dl_state -> (theme key, DESIGN.md hex fallback)
    _STATE_COLOR = {
        "unsaved": ("text_dim", "#91A3B4"),
        "saving": ("primary", "#F5B75F"),
        "saved": ("success", "#5BD98A"),
        "failed": ("error", "#FF6B6B"),
    }

    def __init__(self, kind="file", name="", size=0, blob=b"",
                 player=None, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=dp(40), spacing=dp(6), **kw)
        self._kind = kind
        self._name = name
        self._blob = blob
        self._size = size
        self._player = player
        self.kind_label = Label(
            text="[video]" if kind == "video" else "[file]",
            size_hint_x=None, width=dp(56), font_size=sp(12),
            color=self._theme_color(*self._STATE_COLOR["unsaved"]),
            halign="left", valign="middle")
        self.kind_label.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        self.add_widget(self.kind_label)
        self.name_label = Label(
            text=name or "attachment", size_hint_x=1, font_size=sp(12),
            color=self._theme_color("text", "#F4F0E7"),
            shorten=True, halign="left", valign="middle")
        self.name_label.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        self.add_widget(self.name_label)
        self.size_label = Label(
            text="%d KB" % (size // 1024), size_hint_x=None,
            width=dp(64), font_size=sp(12),
            color=self._theme_color(*self._STATE_COLOR["unsaved"]),
            halign="right", valign="middle")
        self.size_label.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        self.add_widget(self.size_label)
        self.state_label = Label(
            text=self._STATE_TEXT["unsaved"], size_hint_x=None,
            width=dp(88), font_size=sp(12),
            color=self._theme_color(*self._STATE_COLOR["unsaved"]),
            halign="right", valign="middle")
        self.state_label.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        self.add_widget(self.state_label)
        self.bind(on_release=self._on_tap)

    def _theme_color(self, key, fallback_hex):
        """Theme colour for `key`, or the DESIGN.md hex fallback.
        Never raises."""
        try:
            app = App.get_running_app()
            if app is not None:
                colors = getattr(app, "theme_colors", None)
                if colors:
                    value = colors.get(key)
                    if value is not None:
                        return [float(c) for c in list(value)]
        except Exception:
            pass
        try:
            return list(get_color_from_hex(fallback_hex))
        except Exception:
            return [1, 1, 1, 1]

    def on_dl_state(self, *args):
        """Repaint the state label for the new download state."""
        state = self.dl_state
        try:
            self.state_label.text = self._STATE_TEXT.get(state, state)
            self.state_label.color = self._theme_color(
                *self._STATE_COLOR.get(state,
                                       self._STATE_COLOR["unsaved"]))
        except Exception:
            pass

    def _on_tap(self, *args):
        """Tap: show saving, then save on the next Clock tick so the
        saving state is actually visible. A tap while a save is in
        flight is ignored; a tap after saved re-reports."""
        if self.dl_state == "saving":
            return
        self.dl_state = "saving"
        Clock.schedule_once(self._do_save, 0)

    def _do_save(self, dt):
        p = self._player
        if p is None:
            self.dl_state = "failed"
            return
        try:
            path = p._save_and_report(self._name, self._blob, self)
        except Exception:
            path = None
        self.dl_state = "saved" if path else "failed"


def _app_data_dir():
    """Cross-platform writable app data directory.

    On Android (p4a) uses the app's internal storage directory so scoped
    storage (Android 10+) doesn't block writes. On desktop falls back to
    ~/.sHades_client.
    """
    try:
        from android.storage import app_storage_path  # p4a only, no-op on desktop
        return app_storage_path()
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".sHades_client")


def cache_dir():
    d = os.path.join(_app_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cached_path(key):
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32])


# ---------------------------------------------------------------------------
# Image cache (pfps + attachments)
# ---------------------------------------------------------------------------
class ImageCache:
    def __init__(self):
        self._paths = {}   # key -> cached file path
        self._loading = set()

    def get(self, key):
        return self._paths.get(key)

    def begin_load(self, key):
        """Claim the right to fetch `key`. False if it is already cached or
        an in-flight fetch owns it -- that is what keeps a history of many
        messages from one author from issuing one avatar request each."""
        if key in self._paths or key in self._loading:
            return False
        self._loading.add(key)
        return True

    def end_load(self, key):
        """Release a claim taken by begin_load. Call this even when the
        fetch failed, or the key stays blocked forever."""
        self._loading.discard(key)

    def set_bytes(self, key, data, ext="png"):
        try:
            # Keys are "av:<hash>"; ':' is illegal in a Windows filename,
            # so hash the key instead of using it verbatim.
            fpath = cached_path(key) + "." + ext
            with open(fpath, "wb") as f:
                f.write(data)
            self._paths[key] = fpath
            return fpath
        except Exception as e:
            print("image cache error:", e)
            return None

    def forget(self, key):
        """Drop one cache entry and its file (P1 account purge: the
        cached avatar of the account leaving must not survive it)."""
        path = self._paths.pop(key, None)
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError as e:
                print("image cache forget error:", e)

    def fetch_and_cache(self, api, path, key, ext="png"):
        if not self.begin_load(key):
            return self._paths.get(key)

        def work():
            try:
                data = api.fetch_file(path)
                if data:
                    Clock.schedule_once(lambda dt: self.set_bytes(key, data, ext))
            finally:
                self.end_load(key)

        threading.Thread(target=work, daemon=True).start()
        return None


# ---------------------------------------------------------------------------
# Fancy buttons (polish group 5): one button language for the app.
#
# variant maps to the DESIGN.md button tokens. theme_colors is the
# source of truth whenever it has the key; the DESIGN.md hex values
# below are fallbacks only -- theme_colors has no primary_hover,
# accent_hover or error key yet, so those three always resolve to the
# fallback until the theme gains them (see DESIGN.md proposal).
# ---------------------------------------------------------------------------
_FANCY_FALLBACKS = {
    "primary": "#F5B75F",
    "primary_hover": "#FFD090",
    "accent": "#58D6C3",
    "accent_hover": "#7CE5D5",
    "panel": "#111925",      # button-secondary base (bg-raised)
    "msg_own": "#1B2C3A",    # button-secondary hover (bg-overlay)
    "bg_main": "#0B1017",    # button-ghost base
    "error": "#FF6B6B",      # button-danger base
    "sidebar_ctrl": "#172231",  # site-editor bar secondary base
}

# variant -> (base theme key, hover theme key or None, MDButton style).
# danger has no hover token in DESIGN.md: hover keeps the base colour.
# ghost hovers to bg-raised (a faint lift off bg-main). outlined has no
# fill: its base is transparent and its hover is a faint bg-overlay
# tint (OUTLINED_HOVER_ALPHA), never a full fill.
_FANCY_VARIANTS = {
    "primary": ("primary", "primary_hover", "filled"),
    "accent": ("accent", "accent_hover", "filled"),
    "secondary": ("panel", "msg_own", "filled"),
    "danger": ("error", None, "filled"),
    "ghost": ("bg_main", "panel", "filled"),
    "outlined": (None, "msg_own", "outlined"),
}

#: Opacity of a disabled FancyMDButton. KivyMD's own disabled treatment
#: is only a faint state-layer wash (live probe under Xvfb: a disabled
#: filled primary button measured (147, 112, 61) against (161, 121, 62)
#: enabled -- ~9% darker), so the widget dims explicitly instead of
#: relying on it. Opacity (not bg) is the mechanism, so it stays
#: consistent across variants including outlined, whose bg is
#: transparent. Press can never fight it: a disabled button fires no
#: on_press, and disabling cancels any running opacity animation.
DISABLED_OPACITY = 0.45

#: Alpha of the outlined variant's hover tint (faint bg-overlay wash).
OUTLINED_HOVER_ALPHA = 0.35


class FancyMDButton(MDButton, HoverBehavior):
    """A button with hover, press and disabled states (polish group 5).

    ``variant`` is one of ``primary`` / ``accent`` / ``secondary`` /
    ``danger`` / ``ghost`` / ``outlined`` (unknown values fall back to
    ``primary``). Behaviour:

    * hover enter swaps ``md_bg_color`` to the variant's hover token,
      leave restores the base token (hover is a state, not motion, so
      it still applies under REDUCED_MOTION);
    * press runs ``ui_fx.tap_feedback`` (opacity dip to 0.55 + return),
      which no-ops itself under REDUCED_MOTION;
    * disabled dims to DISABLED_OPACITY and keeps the base bg (no hover
      swap while disabled);
    * a missing theme key never raises: the DESIGN.md hex fallback is
      used instead, and every step is guarded so a half-built button
      still renders.

    The button re-tints live when the running app reassigns
    ``theme_colors`` (the same DictProperty rebinding the KV buttons
    use). Text colour is not owned here: KV sets it on the
    ``MDButtonText`` child per button, as before.
    """

    variant = StringProperty("primary")

    def __init__(self, **kw):
        self._hovered = False
        self._theme_app = None
        super().__init__(**kw)
        try:
            self.theme_bg_color = "Custom"
        except Exception:
            pass
        try:
            self.bind(on_press=self._on_press_feedback)
        except Exception:
            pass
        self._apply_variant()
        self._apply_state()

    # -- theme ---------------------------------------------------------
    def _theme_rgba(self, key, fallback_hex=None):
        """Theme colour for `key`, or the DESIGN.md hex fallback.

        Never raises and never returns None: a missing app, a missing
        dict, a missing key or a malformed value all land on the
        fallback (opaque white if even that is unusable).
        """
        try:
            app = App.get_running_app()
            if app is not None:
                colors = getattr(app, "theme_colors", None)
                if colors:
                    value = colors.get(key)
                    if value is not None:
                        return [float(c) for c in list(value)]
        except Exception:
            pass
        try:
            return list(get_color_from_hex(fallback_hex or "#FFFFFF"))
        except Exception:
            return [1, 1, 1, 1]

    def _bind_theme(self):
        """Bind once to the running app's theme_colors, if any.

        Called on every state application so a button built before the
        app existed still picks up live re-tints once it appears.
        """
        try:
            app = App.get_running_app()
            if app is not None and app is not self._theme_app:
                app.bind(theme_colors=self._on_theme_changed)
                self._theme_app = app
        except Exception:
            pass

    def _on_theme_changed(self, *args):
        self._apply_state()

    # -- variant / state ------------------------------------------------
    def _variant_spec(self):
        return _FANCY_VARIANTS.get(self.variant,
                                   _FANCY_VARIANTS["primary"])

    def on_variant(self, *args):
        self._apply_variant()
        self._apply_state()

    def _apply_variant(self):
        try:
            self.style = self._variant_spec()[2]
        except Exception:
            pass

    def _base_bg(self):
        # A per-button base-key override (set as _base_key): the
        # site-editor bar's secondary buttons keep the sidebar_ctrl
        # fill the sitenav tests pin, instead of the secondary
        # variant's panel base. Hover still uses the variant's key.
        base_key = getattr(self, "_base_key", None)
        if base_key is None:
            base_key = self._variant_spec()[0]
        if base_key is None:
            return [0, 0, 0, 0]
        return self._theme_rgba(base_key,
                                _FANCY_FALLBACKS.get(base_key, "#FFFFFF"))

    def _hover_bg(self):
        _base_key, hover_key, _style = self._variant_spec()
        if hover_key is None:
            return self._base_bg()
        rgba = self._theme_rgba(hover_key, _FANCY_FALLBACKS[hover_key])
        if self.variant == "outlined":
            rgba = list(rgba)
            rgba[3] = OUTLINED_HOVER_ALPHA
        return rgba

    def _apply_state(self):
        """Paint the bg for the current (disabled / hovered / base) state."""
        self._bind_theme()
        try:
            self.theme_bg_color = "Custom"
        except Exception:
            pass
        try:
            if self.disabled or not self._hovered:
                self.md_bg_color = self._base_bg()
            else:
                self.md_bg_color = self._hover_bg()
        except Exception:
            pass

    # -- hover ----------------------------------------------------------
    def on_enter(self, *args):
        try:
            super().on_enter(*args)
        except Exception:
            pass
        self._hovered = True
        self._apply_state()

    def on_leave(self, *args):
        try:
            super().on_leave(*args)
        except Exception:
            pass
        self._hovered = False
        self._apply_state()

    # -- disabled -------------------------------------------------------
    def on_disabled(self, instance, value):
        try:
            super().on_disabled(instance, value)
        except Exception:
            pass
        try:
            from kivy.animation import Animation
            Animation.cancel_all(self, "opacity")
        except Exception:
            pass
        try:
            self.opacity = DISABLED_OPACITY if value else 1.0
        except Exception:
            pass
        self._apply_state()

    # -- press ----------------------------------------------------------
    def _on_press_feedback(self, *args):
        if self.disabled:
            return
        try:
            if ui_fx is not None:
                ui_fx.tap_feedback(self)
        except Exception:
            pass

    # -- Button-compatible reads ---------------------------------------
    # Plain Kivy Buttons expose .background_color / .color; MDButton
    # does not (the bg lives on md_bg_color, the text colour on the
    # MDButtonText child). These proxies keep code written against the
    # Button API working -- the site-editor bar's tests read them --
    # without duplicating state: every read goes to the live widget.
    # Neither name exists anywhere in this class's MRO, so the plain
    # properties cannot shadow a Kivy property. .text is deliberately
    # NOT proxied: the audio-row test pins the button itself textless
    # (the label child owns the text); the site-editor bar sets a plain
    # .text attribute on its own three buttons instead.
    @property
    def background_color(self):
        try:
            return list(self.md_bg_color)
        except Exception:
            return [1, 1, 1, 1]

    @background_color.setter
    def background_color(self, value):
        try:
            self.md_bg_color = value
        except Exception:
            pass

    @property
    def color(self):
        label = getattr(self, "_fancy_label", None)
        if label is not None:
            try:
                return list(label.text_color)
            except Exception:
                pass
        return [1, 1, 1, 1]

    @color.setter
    def color(self, value):
        label = getattr(self, "_fancy_label", None)
        if label is not None:
            try:
                label.text_color = value
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
class ContextMenuMixin:
    """Right-click (desktop) / long-press (touch) context menu, Discord-style.

    Subclasses implement `build_context_menu()` returning a list of
    (label, enabled, callable). Right-click opens immediately; a 0.5 s hold
    opens it on touch (mouse left-press never does). One code path, two
    gestures; the menu itself is the app's Window-level overlay.

    NOTE: this class must be defined BEFORE the widgets that inherit from
    it (MessageRow, ChannelButton, ...). A base class is evaluated at class
    -definition time, so a mixin defined further down the file makes the
    module fail to import with NameError."""

    LONG_PRESS_S = 0.5

    def build_context_menu(self):
        return []

    def _open_ctx_menu(self, window_pos):
        app = App.get_running_app()
        if app is not None:
            app.open_context_menu(self.build_context_menu(), window_pos)

    def on_touch_down(self, touch):
        if touch.grab_current is self:
            return super().on_touch_down(touch)
        if getattr(touch, "button", None) == "right":
            # desktop right-click: menu instead of the left-press action
            self._open_ctx_menu(self.to_window(touch.x, touch.y))
            return True
        if "button" not in getattr(touch, "profile", []) or \
                touch.profile == []:
            # real touch (finger): long-press opens the menu
            touch.ud["shades_ctx_pos"] = self.to_window(touch.x, touch.y)
            touch.ud["shades_ctx_fired"] = False
            touch.grab(self)
            self._ctx_clock = Clock.schedule_once(
                lambda dt: self._ctx_fire(touch), self.LONG_PRESS_S)
        return super().on_touch_down(touch)

    def _ctx_fire(self, touch):
        touch.ud["shades_ctx_fired"] = True
        self._open_ctx_menu(touch.ud.get("shades_ctx_pos")
                            or self.to_window(*self.center))

    def _ctx_is_button(self):
        # True when this widget is also ButtonBehavior-backed. Checked here
        # and not at class definition so a non-kivy import (lint) survives.
        from kivy.uix.behaviors.button import ButtonBehavior
        return isinstance(self, ButtonBehavior)

    def on_touch_up(self, touch):
        clock = getattr(self, "_ctx_clock", None)
        if clock:
            clock.cancel()
            self._ctx_clock = None
        if touch.ud.get("shades_ctx_fired"):
            # the long-press fired: swallow the corresponding up
            touch.ud["shades_ctx_fired"] = False
            touch.ungrab(self)
            touch.ud["shades_ctx_up_done"] = True
            return True
        if self._ctx_is_button() and self not in touch.ud:
            # ButtonBehavior's on_touch_down was short-circuited (a child
            # widget consumed the press), so only the mixin's grab entry
            # exists. Forwarding this up would enter ButtonBehavior.on_touch_up
            # through the grab pass without its down-state: `time() -
            # self.__touch_time` at kivy/uix/behaviors/button.py:173 turns
            # float - None (caught on-device as the 1137 crash; desktop asserts
            # first). The consuming child receives its own up from its own
            # grab entry, so swallowing here starves nothing.
            touch.ungrab(self)
            touch.ud["shades_ctx_up_done"] = True
            return True
        if touch.ud.get("shades_ctx_up_done"):
            # The mixin grab plus ButtonBehavior's own grab put this widget in
            # the touch's grab list twice, and kivy delivers the up once per
            # entry (base.py post_dispatch_input). One release is the
            # contract; the second delivery would double-fire on_release.
            return True
        result = super().on_touch_up(touch)
        if result:
            touch.ud["shades_ctx_up_done"] = True
        return result


class ContextMenuOverlay(FloatLayout):
    """Full-screen overlay hosting one context menu; a tap anywhere outside
    the menu dismisses it, a tap on an item acts then dismisses. Parented to
    Window by the app."""

    def __init__(self, actions, window_pos, **kw):
        super().__init__(**kw)
        menu = BoxLayout(orientation="vertical", size_hint=(None, None),
                         spacing=dp(1),
                         size=(dp(230), dp(36) * max(1, len(actions))))
        menu.pos = (max(dp(4), min(window_pos[0],
                                   Window.width - menu.width - dp(4))),
                    max(dp(4), min(window_pos[1] - menu.height,
                                   Window.height - menu.height - dp(4))))
        for label, enabled, cb in actions:
            btn = Button(text=label, size_hint_y=None, height=scaled(dp(34)),
                         background_color=(0.18, 0.2, 0.24, 1) if enabled
                         else (0.12, 0.13, 0.15, 1),
                         color=(0.9, 0.92, 0.95, 1) if enabled
                         else (0.4, 0.42, 0.46, 1),
                         font_size=sp(13))
            if enabled:
                btn.bind(on_press=lambda inst, cb=cb: self._run(cb))
            else:
                btn.disabled = True
            menu.add_widget(btn)
        self.add_widget(menu)
        self.menu = menu

    def on_touch_down(self, touch):
        # overlay covers the window, so window coords are local coords
        inside = self.menu.collide_point(*touch.pos)
        if not inside:
            self.dismiss_now()
            return True
        # let the button get its press; remove the overlay right after
        Clock.schedule_once(lambda dt: self.dismiss_now(), 0.2)
        return super().on_touch_down(touch)

    def dismiss_now(self, *a):
        parent = self.parent
        if parent is not None:
            parent.remove_widget(self)

    def _run(self, cb):
        try:
            cb()
        finally:
            Clock.schedule_once(lambda dt: self.dismiss_now(), 0.1)


class MeshPulse(Widget):
    """A path-state ring; pace and colour reflect the observed guild link."""
    mode = StringProperty("offline")
    phase = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint = (None, None)
        self.size = (dp(40), dp(40))
        self.bind(pos=self._draw, size=self._draw, mode=self._draw,
                  phase=self._draw)
        self._pulse_clock = Clock.schedule_interval(self._tick, 0.1)

    def _tick(self, dt):
        speed = 0.0 if self.mode == "offline" else (0.11 if self.mode == "relay" else 0.25)
        self.phase = (self.phase + speed * dt) % 1.0

    def _draw(self, *a):
        from math import sin, pi
        palette = {"direct": (0.35, 0.84, 0.77),
                   "relay": (0.96, 0.72, 0.37),
                   "offline": (0.38, 0.43, 0.48)}
        rgb = palette.get(self.mode, palette["offline"])
        wave = 0.0 if self.mode == "offline" else (sin(self.phase * 2 * pi) + 1) / 2
        self.canvas.clear()
        with self.canvas:
            Color(*rgb, 0.17 + 0.20 * wave)
            Line(circle=(self.center_x, self.center_y, min(self.width, self.height) * 0.42),
                 width=dp(4))
            Color(*rgb, 0.85)
            Line(circle=(self.center_x, self.center_y, min(self.width, self.height) * 0.34),
                 width=dp(1.4))
            Color(*rgb, 0.8)
            Ellipse(pos=(self.center_x - dp(3), self.center_y - dp(3)),
                    size=(dp(6), dp(6)))


class MeshConstellation(Widget):
    """A compact path diagram. Dots are capped when the route is longer."""
    mode = StringProperty("offline")
    hops = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint = (None, None)
        self.size = (dp(52), dp(20))
        self.bind(pos=self._draw, size=self._draw, mode=self._draw,
                  hops=self._draw)

    def _draw(self, *a):
        count = max(2, min(5, int(self.hops) + 1))
        rgb = ((0.35, 0.84, 0.77) if self.mode == "direct"
               else (0.96, 0.72, 0.37) if self.mode == "relay"
               else (0.38, 0.43, 0.48))
        points = [(self.x + dp(5) + i * (self.width - dp(10)) / (count - 1),
                   self.center_y + (dp(3) if i % 2 else -dp(3)))
                  for i in range(count)]
        self.canvas.clear()
        with self.canvas:
            Color(*rgb, 0.4 if self.mode != "offline" else 0.2)
            Line(points=[c for p in points for c in p], width=dp(1))
            for i, (x, y) in enumerate(points):
                Color(*rgb, 0.85 if self.mode != "offline" or i == 0 else 0.3)
                Ellipse(pos=(x - dp(2.5), y - dp(2.5)), size=(dp(5), dp(5)))


class SkeletonRow(Widget):
    """Two quiet shimmer bars while a history page is on its way.

    Polish group 8b: the shimmer honours REDUCED_MOTION, mirroring
    ui_fx.SkeletonRow -- with it on, the bars render statically (no
    repeating phase animation). A row built while motion is on never
    starts the animation; one already shimmering stops the moment
    set_reduced_motion() flips. (ui_fx is imported at module level in
    the Kivy try block; getattr keeps the Kivy-absent stub path safe.)
    """
    phase = NumericProperty(0)
    _instances = WeakSet()

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint_y = None
        self.height = dp(70)
        self.bind(pos=self._draw, size=self._draw, phase=self._draw)
        self._anim = None
        self._instances.add(self)
        self._sync_motion()

    def _sync_motion(self):
        if getattr(ui_fx, "REDUCED_MOTION", False):
            self._stop()
        else:
            self._start()

    def _start(self):
        if getattr(ui_fx, "REDUCED_MOTION", False):
            return
        self._stop()
        from kivy.animation import Animation
        self._anim = Animation(phase=1, duration=1.6, t="linear")
        self._anim.repeat = True
        self._anim.start(self)

    def _stop(self):
        if self._anim is not None:
            self._anim.cancel(self)
            self._anim = None

    def _draw(self, *a):
        self.canvas.clear()
        width = max(dp(40), self.width - dp(72))
        with self.canvas:
            Color(0.64, 0.75, 0.76, 0.12)
            Ellipse(pos=(self.x + dp(12), self.y + dp(18)), size=(dp(34), dp(34)))
            RoundedRectangle(pos=(self.x + dp(58), self.y + dp(44)),
                             size=(width * 0.45, dp(10)), radius=[dp(5)] * 4)
            RoundedRectangle(pos=(self.x + dp(58), self.y + dp(21)),
                             size=(width * 0.8, dp(10)), radius=[dp(5)] * 4)
            # Short highlight moves within the body bar.
            Color(0.70, 0.92, 0.89, 0.13)
            RoundedRectangle(pos=(self.x + dp(58) + width * 0.64 * self.phase,
                                  self.y + dp(21)),
                             size=(width * 0.16, dp(10)), radius=[dp(5)] * 4)


class MicronRefLabel(Label):
    """Markup label whose ref taps survive nesting inside a ScrollView.

    Kivy 2.3.1 walks site pages 2+ levels under the messages ScrollView
    (scroll > GridLayout > page container > label), and the touch reaches
    those widgets in the SCROLL's content frame (scrollview.py to_local
    folds a single g_translate over the whole subtree; the walk applies no
    per-hop pos transforms). Label.on_touch_down's ref check assumes its
    own parent space, so the zones miss by the accumulated offsets and the
    tap dies (0.36.x device receipt: [ Edit page ] did nothing; the client
    split dropped the pre-split MicronRefLabel when it moved the rows to
    plain kv labels - fold-18 restored this class verbatim from the
    pre-split base).

    Convert the delivered frame by subtracting this label's own offset
    chain up to the wrapping ScrollView, then run kivy's ref-zone check
    with the same top-down texture y that label.py uses. The scroll's own
    translation stays in the delivered frame, so stopping the walk there
    is both correct and the only cheap anchor kivy gives us.
    """

    def _content_anchor(self):
        ox, oy = self.x, self.y  # offsets up to (and incl.) this label
        node = self.parent
        while node is not None and type(node).__name__ != "ScrollView":
            ox += node.x
            oy += node.y
            node = node.parent
        return ox, oy

    def on_touch_down(self, touch):
        _dbg = os.environ.get("TAPWALK")
        if _dbg:
            print("REFLBL delivered=%r anchor=%r tex=%r size=%r" % (
                tuple(round(v, 1) for v in touch.pos),
                tuple(round(v, 1) for v in self._content_anchor()),
                tuple(self.texture_size), tuple(self.size)), flush=True)
        if super().on_touch_down(touch):
            return True
        if not len(self.refs):
            return False
        ox, oy = self._content_anchor()
        lx = touch.x - ox
        ly = touch.y - oy
        tex_h = self.texture_size[1] or 18.0
        ty = tex_h - ly  # label zones are top-down like label.py's math
        if _dbg:
            print("REFLBL local=(%.1f, %.1f) ty=%.1f zones=%r" % (
                lx, ly, ty, self.refs), flush=True)
        for uid, zones in self.refs.items():
            for zone in zones:
                x, y, w, h = zone
                if x <= lx <= w and y <= ty <= h:
                    self.dispatch('on_ref_press', uid)
                    return True
        return False


class ClickableLabel(ContextMenuMixin, ButtonBehavior, Label):
    """Username label: tap opens a profile, right-click/long-press offers
    mention + profile."""
    target_hash = StringProperty("")

    def build_context_menu(self):
        app = App.get_running_app()
        name = self.text or ""
        actions = [("open profile", True,
                    lambda: app and app.open_profile(
                        self.target_hash, name)),
                   ("mention @%s" % name, True,
                    lambda: app and app._mention(name))]
        return actions


class _MiniTap(ButtonBehavior, Label):
    """Tiny tappable text: press fires the callback (down=True); the
    hold variant also fires release (down=False) for push-to-talk."""

    def __init__(self, **kw):
        self._hold = kw.pop("hold", False)
        self._cb = kw.pop("cb", None)
        super().__init__(font_size=sp(10), halign="left", valign="middle",
                         **kw)
        self.bind(size=lambda inst, s: setattr(inst, "text_size",
                                               (s[0], s[1])))

    def on_press(self):
        if self._cb is not None:
            self._cb(True)

    def on_release(self):
        if self._cb is not None and self._hold:
            self._cb(False)


VOICE_MODE_CHOICES = (("always", "always"), ("ptt", "ptt"), ("voc", "vad"))


class VoiceRow(BoxLayout):
    """One voice channel's roster row on the rail (item 6).

    Head line ("n in call") plus one line per connected member. tay
    2026-11-25: connected users list under the channel, and the member
    line turns green while that peer transmits (voice dest inside the
    speaking set; the you-row is green while this client's own tx is
    live). While joined, a mini row picks the tx mode - always / ptt /
    voc (vad) - and ptt adds a hold control. Press on the row routes to
    the app's voice panel actions through bind_action; while in-call,
    Mute/Unmute joins the panel's state (double-tap).
    """

    channel_name = StringProperty("")

    GREEN = [0.30, 0.85, 0.42, 1]
    DIM = [0.55, 0.55, 0.55, 1]

    def __init__(self, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=dp(22), padding=(dp(24), 0, dp(6), 0),
                         **kw)
        self._label = Label(font_size=sp(10),
                            color=App.get_running_app().theme_colors
                            ["text_muted"] if App.get_running_app()
                            else (0.6, 0.6, 0.6, 1),
                            halign="left", valign="middle")
        self._label.bind(size=lambda inst, s: setattr(inst, "text_size",
                                                      (s[0], s[1])))
        self._cta = Label(text="join", font_size=sp(11))
        self.add_widget(self._label)
        self.add_widget(self._cta)
        self._members = []
        self._joined = False
        self._muted = False
        self._member_lines = []
        self._mode_widgets = None
        self._speaking = set()
        self._tx_mode = None
        self._self_speaking = False
        self._my_viewer = ""
        self._on_join = self._on_leave = self._on_mute = None
        self._on_mode = self._on_ptt = None
        self.bind(on_touch_down=self._tap_dispatch)

    def bind_action(self, on_join, on_leave, on_mute, on_mode=None,
                    on_ptt=None):
        self._on_join, self._on_leave, self._on_mute = (
            on_join, on_leave, on_mute)
        self._on_mode = on_mode
        self._on_ptt = on_ptt

    def render(self, members, joined=False, muted=False, speaking=None,
               tx_mode=None, self_speaking=False, my_viewer=""):
        self._members = members or []
        self._joined = joined
        self._muted = muted
        self._speaking = set(speaking or [])
        self._tx_mode = tx_mode
        self._self_speaking = self_speaking
        self._my_viewer = my_viewer or ""
        names = ", ".join(str(m.get("viewer", ""))[:4]
                          for m in self._members[:3])
        count = len(self._members)
        head = ("%d in call" % count) if count else "empty"
        self._label.text = ("%s - %s%s" % (head, names,
                                           ", ..." if count > 3 else "")
                            if names else head)
        self._cta.text = ("leave" if self._joined else
                          "join")
        self._render_modes()

    def _render_modes(self):
        want = self._joined and self._on_mode is not None
        if want and self._mode_widgets is None:
            self._build_modes()
        if self._mode_widgets is not None and not want:
            self.remove_widget(self._mode_widgets["row"])
            self._mode_widgets = None
        elif self._mode_widgets is not None:
            mode = self._tx_mode or "always"
            for label, m in VOICE_MODE_CHOICES:
                self._mode_widgets["btns"][label].color = \
                    self.GREEN if m == mode else self.DIM
        self._render_members()

    def _render_members(self):
        """One line per connected member; green while transmitting."""
        want = min(len(self._members), 4)
        while len(self._member_lines) < want:
            lbl = Label(font_size=sp(9), halign="left", valign="middle",
                        color=self.DIM)
            lbl.bind(size=lambda inst, s: setattr(
                inst, "text_size", (s[0], s[1])))
            self.add_widget(lbl)
            self._member_lines.append(lbl)
        for idx, lbl in enumerate(self._member_lines):
            if idx >= want:
                lbl.text = ""
                continue
            m = self._members[idx]
            viewer = str(m.get("viewer", ""))
            is_me = bool(self._my_viewer) and viewer.startswith(
                self._my_viewer)
            dest = m.get("dest") or ""
            hot = bool(dest) and dest in self._speaking
            tag = "you" if is_me else viewer[:8]
            if is_me and self._joined and self._self_speaking:
                hot = True
            lbl.text = ("   %s - speaking" % tag) if hot \
                else ("   %s" % tag)
            lbl.color = self.GREEN if hot else self.DIM
        self.height = dp(22) + dp(15) * want \
            + (dp(48) if self._mode_widgets is not None else 0)

    def _build_modes(self):
        row = BoxLayout(orientation="vertical", size_hint_y=None,
                        height=dp(48), padding=(dp(24), 0, dp(6), 0))
        picks = BoxLayout(orientation="horizontal", size_hint_y=None,
                          height=dp(22), spacing=dp(6))
        btns = {}
        for label, m in VOICE_MODE_CHOICES:
            t = _MiniTap(text=label, color=self.DIM, size_hint_x=None,
                         width=dp(38),
                         cb=(lambda down, m=m: self._on_mode(
                             self.channel_name, m)))
            btns[label] = t
            picks.add_widget(t)
        hold = _MiniTap(text="hold (ptt)", color=self.GREEN,
                        size_hint_x=None, width=dp(74), hold=True,
                        cb=lambda down: self._on_ptt(self.channel_name,
                                                     down))
        row.add_widget(picks)
        row.add_widget(hold)
        self.add_widget(row)
        self._mode_widgets = {"row": row, "btns": btns, "hold": hold}

    def _tap_dispatch(self, inst, touch):
        if not self.collide_point(*touch.pos):
            return False
        # one-tap CTA: in-call -> leave; not in-call -> join. The mute
        # toggle lives on the in-call panel, not the rail row.
        if self._joined:
            if self._on_mute is not None and touch.is_double_tap:
                self._on_mute(self.channel_name)
            elif self._on_leave is not None:
                self._on_leave(self.channel_name)
        elif self._on_join is not None:
            self._on_join(self.channel_name)
        return True


class ChannelButton(ContextMenuMixin, ButtonBehavior, BoxLayout):
    channel_name = StringProperty("")
    badge = NumericProperty(0)
    selected = BooleanProperty(False)
    _dragging = False   # set True once a grabbed touch passes dp(8)

    def build_context_menu(self):
        app = App.get_running_app()
        name = self.channel_name
        cid = app._channels.get(name) if app else None
        actions = [("open #" + name, True,
                    lambda: app and app._switch_channel(name))]
        if app and app._can_create_channels():
            actions.append(("create channel...", True,
                            lambda: app and app._open_create_channel_popup()))
        if cid:
            if getattr(app, "i_am_admin", False):
                actions.append(("invite to #%s..." % name, True,
                                lambda: app and app._invite_to_channel(cid, name)))
            actions.append(("copy channel id", True,
                            lambda: app and app._copy_text(cid)))
            muted = cid in app._muted_channels
            actions.append(("unmute channel" if muted else "mute channel", True,
                            lambda: app and app._toggle_mute_channel(name, cid)))
            if app and app.i_am_moderator:
                actions.append(("delete channel", True,
                                lambda: app and app._delete_channel(name, cid)))
        return actions

    # ---- drag reorder (task 7) --------------------------------------------
    # A grabbed touch that moves more than dp(8) cancels the long-press
    # clock (tay: presses over a channel label belong to the drag/menu
    # logic, never to the drawer) and reorders the grid live; on release
    # the final order goes to /api/v1/channels/reorder once.
    def on_touch_down(self, touch):
        touch.ud["shades_drag_origin"] = (touch.x, touch.y)
        self._dragging = False
        return super().on_touch_down(touch)

    def _ch_drag_check(self, touch):
        """One move step: threshold -> live reorder. Returns True when the
        touch has become a drag."""
        origin = touch.ud.get("shades_drag_origin")
        if origin is None:
            return False
        dx, dy = touch.x - origin[0], touch.y - origin[1]
        if not getattr(self, "_dragging", False):
            if dx * dx + dy * dy < dp(8) * dp(8):
                return False
            self._dragging = True
            clock = getattr(self, "_ctx_clock", None)
            if clock:
                clock.cancel()
                self._ctx_clock = None
            # The owning ScrollView has the touch: take it back so the
            # list stops panning under the drag (v0.32 device note).
            node = self.parent
            while node is not None \
                    and node.__class__.__name__ != "ScrollView":
                node = node.parent
            if node is not None:
                node._touch = None
                try:
                    touch.ungrab(node)
                except Exception:
                    pass
        self._ch_drag_step(touch)
        return True

    def on_touch_move(self, touch):
        # v0.32 (tay device note): the channel list's ScrollView grabs
        # every press, so grab_current never names the button and this
        # branch never delivered moves; the origin stamp on the touch is
        # ours (set in on_touch_down), accept moves on that instead.
        if touch.ud.get("shades_drag_origin") is not None:
            self._ch_drag_check(touch)
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        was_dragging = getattr(self, "_dragging", False)
        self._dragging = False
        result = super().on_touch_up(touch)
        if was_dragging:
            app = App.get_running_app()
            if app is not None:
                app._post_channel_reorder()
            return True  # a drag end is consumed, not a press
        return result

    def _ch_drag_step(self, touch):
        """Swap with the neighbouring row when the pointer crosses it."""
        grid = self.parent
        if grid is None:
            return
        buttons = [w for w in reversed(grid.children)
                   if isinstance(w, ChannelButton)]
        if len(buttons) < 2:
            return
        top = grid.top
        row_h = 0.0
        for b in buttons:
            row_h = b.height
        my_row = None
        for i, b in enumerate(buttons):
            if b is self:
                my_row = i
        if my_row is None or not row_h:
            return
        # which row does the pointer sit in?
        offset = int(round((top - touch.y - row_h / 2.0) / row_h))
        target = max(0, min(len(buttons) - 1, offset))
        if target == my_row:
            return
        # move self within the children list so the visual order changes
        # GridLayout fills rows in reversed-children order: children index
        # (len-1-row) is row 0 (top).
        child_at_row = len(buttons) - 1 - target
        grid.remove_widget(self)
        grid.add_widget(self, index=child_at_row)
