# sHades Kivy client — Discord-like UI over Reticulum/LXMF
#
# Connects to an sHades guild (sHades.py) over RNS: establishes a link to the
# guild's nomadnetwork.node destination, identifies, and polls the guild's
# /api/* endpoints (msgpack) for messages, state, avatars and attachments.
#
# UI (modeled after Discord):
#   - left  : guild channel list (collapsable)
#   - center: active channel messages + input bar
#   - right : channel member list (collapsable)
#
# Targets: Linux, Windows, Android. See .github/workflows/build.yml.

import os
import sys
import re
import json
import time
import base64
import hashlib
import shutil
import tempfile
import threading
import random
from collections import OrderedDict
from PIL import Image as PILImage
from PIL import ImageDraw as PILImageDraw
from PIL import ImageChops as PILImageChops

from ui_scale import scaled
from mesh_ui import grouped_with, link_state, reply_wire, split_reply
from ui_theme import preset_colors
from client_util import (  # noqa: F401  (rgba_to_hex: kc.* tests)
    ATT_EPHEMERAL, ATT_SERVER, _escape, dlog, hex_to_rgba, hexstr,
    md_to_kivy, now, parse_slash_help, rgba_to_hex,
)
import client_util

# ---------------------------------------------------------------------------
# P11 version bridge: the running build knows its own version.
# ---------------------------------------------------------------------------
APP_VERSION = "0.36.25"

# buildozer.spec, kivy_client.APP_VERSION and sHades.SERVER_VERSION must all
# match (tests/test_update_meta.py keeps them in lockstep). On-device the
# APK's own versionName (PackageManager) wins, so a sideloaded build always
# reports what it really is; everywhere else the constant answers.
def _client_version():
    try:
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        info = activity.getPackageManager().getPackageInfo(
            activity.getPackageName(), 0)
        v = str(info.versionName)
        return v if v else APP_VERSION
    except Exception:
        return APP_VERSION

# ---------------------------------------------------------------------------
# Kivy (guarded so the module can be imported for linting without Kivy present)
# ---------------------------------------------------------------------------
def _make_kivy_stub(name, err=None):
    # Module-scope so BOTH guard branches can bind stub names. Each name
    # gets its own distinct stub class (the client stacks several in the
    # same bases tuple; two occurrences of one class make that definition
    # a TypeError). Import-only: instantiating one raises, embedding `err`
    # -- the real import failure -- so a phone death names its cause.
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
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.label import Label
    from kivy.uix.image import Image
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
    from kivy.core.window import Window
    from kivy.clock import Clock
    from kivy.properties import (
        StringProperty, ObjectProperty, BooleanProperty, NumericProperty,
        DictProperty, ListProperty,
    )
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.uix.filechooser import FileChooserListView
    from kivy.uix.popup import Popup
    from kivy.uix.button import Button
    from kivy.uix.textinput import TextInput
    from kivy.uix.widget import Widget
    from kivy.graphics import Color, Ellipse, Line, Rectangle
    from kivy.metrics import dp, sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e

    # Minimal stubs so module-scope class definitions (below) can evaluate
    # when Kivy is absent. They are import-only: instantiating one raises,
    # so nothing silently pretends to be a widget. Client test suites that
    # need real widgets already SKIP on their own via kc.KIVY.
    FloatLayout = _make_kivy_stub("FloatLayout", _e)
    BoxLayout = _make_kivy_stub("BoxLayout", _e)
    Label = _make_kivy_stub("Label", _e)
    ButtonBehavior = _make_kivy_stub("ButtonBehavior", _e)
    App = _make_kivy_stub("App", _e)
    MDApp = _make_kivy_stub("MDApp", _e)

    def _stub_property(*args, **kwargs):
        return None

    StringProperty = ObjectProperty = NumericProperty = _stub_property
    BooleanProperty = DictProperty = ListProperty = _stub_property

# KivyMD imports (only available if Kivy is present)
if KIVY:
    try:
        from kivymd.app import MDApp
        KIVYMD = True
    except Exception as _e:
        # The exception path MUST bind MDApp: the module-level
        # `class sHadesClientApp(MDApp):` evaluates at import time, and an
        # unbound name here was the exact cause of tay's phone crash
        # (NameError: name 'MDApp' is not defined, kivy_client.py:1643).
        # The stub embeds the real kivymd failure, so the death later --
        # at app instantiation -- NAMES the cause instead of a bare
        # NameError.
        MDApp = _make_kivy_stub("MDApp", _e)
        KIVYMD = False
        _KIVYMD_IMPORT_ERROR = _e
        # The tee (main.py) captures stdout from process start, so this
        # line lands in sHades_boot.log and the on-screen crash console:
        # it names the actual kivymd-side failure on-device.
        print("PYTHON: kivymd import failed: %r" % (_e,), flush=True)
else:
    KIVYMD = False

# ---------------------------------------------------------------------------
# TEMP boot diagnostics (v0.4): window-level heartbeat overlay + boot log.
# v0.3 lesson: a heartbeat inside a screen's root layout is at the mercy of
# that layout -- a vertical BoxLayout can squeeze it to nothing or push it
# below the fold, which reads as "no heartbeat". The overlay now hangs on
# the Kivy Window itself with an explicit pos, so no layout can move it.
# Every milestone is ALSO appended to sHades_boot.log (external files dir
# preferred, app-storage fallback) alongside the stdout/stderr tee that
# main.py installs at process start, so the launch story survives even a
# dead renderer. Remove BOOT_DEBUG once the launch diagnosis is done.
# ---------------------------------------------------------------------------
BOOT_DEBUG = True  # v0.36.24 DIAG: milestones ride the
# user-grabbable log (the 0.36.22 crasher got only tee lines)

_boot_lines = []
_boot_label = {"ref": None}

# The IME lifting meter, v0.36.15: resolved jnius handles for the
# kivy-thread visible-frame poll (None until the first tick), plus the
# once-only report set for failures (a failing tick must not spam the
# boot log).
_FRAME_METER = None
_FRAME_REPORTED = set()


def _softinput_reported_once(tag):
    """True the SECOND time a tag is seen; the first reports."""
    if tag in _FRAME_REPORTED:
        return True
    _FRAME_REPORTED.add(tag)
    return False


def _boot_file(msg):
    """Append a milestone to the boot log file, then mirror it
    into the user-visible Downloads collection (adb-free crash
    triage; every milestone refreshes the mirror while
    BOOT_DEBUG is on). Never raises."""
    try:
        from main import _log_path
        p = _log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    try:
        from main import _export_bootlog_for_user
        _export_bootlog_for_user()
    except Exception:
        pass


def _boot_mark(msg):
    """Record a boot milestone; drawn onto the overlay once it exists."""
    if not BOOT_DEBUG:
        return
    _boot_lines.append(msg)
    _boot_file("BOOT %s" % msg)
    _boot_refresh()


def _boot_refresh():
    lbl = _boot_label["ref"]
    if lbl is not None:
        lbl.text = "\n".join(_boot_lines)


def _boot_install(screen=None):
    """Hang the heartbeat on the Kivy Window itself (explicit pos: no
    layout can squeeze or move it); auto-hides after 90 s."""
    from kivy.clock import Clock

    lbl = Label(
        text="\n".join(_boot_lines), color=(1, 0.85, 0.4, 1),
        font_size="10sp", halign="left", valign="top", markup=False,
        size_hint=(None, None),
    )
    _boot_label["ref"] = lbl

    def _place(*a):
        lbl.size = (min(max(lbl.texture_size[0], 1), 420),
                    max(lbl.texture_size[1], 1))
        lbl.pos = (24, Window.height - 26 - lbl.height)

    lbl.bind(texture_size=_place)
    Window.bind(size=_place)
    Window.add_widget(lbl)
    lbl.text = "\n".join(_boot_lines)
    _place()

    Clock.schedule_once(lambda dt: _boot_hide(), 90)


def _boot_hide():
    lbl = _boot_label["ref"]
    if lbl is not None and lbl.parent is not None:
        lbl.parent.remove_widget(lbl)
    _boot_label["ref"] = None


import RNS  # noqa: E402  (after the guarded kivy/diagnostics block, by design)
import invite  # noqa: E402
import dms  # noqa: E402
import recovery  # noqa: E402
import qr_tools  # noqa: E402
import offline_store  # noqa: E402
import profile_card  # noqa: E402
import ui_geom  # noqa: E402
from guild_api import GuildAPI  # noqa: E402
from client_widgets import (  # noqa: E402, F401
    _AudioAttachRow, ImageCache, ContextMenuMixin, ContextMenuOverlay,
    MeshPulse, MeshConstellation, SkeletonRow, FancyMDButton,
    ClickableLabel, ChannelButton, _app_data_dir, cache_dir,
)  # noqa: E402, F401
from app_channels import ChannelsMixin  # noqa: E402
from app_settings import SettingsMixin  # noqa: E402
from app_site import SiteMixin  # noqa: E402
from app_stream import StreamMixin  # noqa: E402
from app_voice_panel import VoicePanelMixin  # noqa: E402
from app_menus import MenusMixin  # noqa: E402
from app_attachments import AttachmentsMixin  # noqa: E402


def _blob_sniff(raw):
    """Sniff raw asset bytes for an SDL-image-loadable format.

    Returns (ext, renderable). Unknown formats keep the bytes off
    every Image widget (the strip renders the letter tile instead):
    naming them .png just trades a missing banner for a broken one.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", True
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg", True
    if raw[:4] == b"GIF8":
        return "gif", True
    if raw[:2] == b"BM":
        return "bmp", True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", True
    return "bin", False


if KIVY:
    from app_site import MessageEmbed  # noqa: E402, F401

# TEMP boot diagnostics: the absence of this line in sHades_boot.log
# pins a crash INSIDE the import chain above (RNS / LXMF / micron...).
_kv_mod = sys.modules.get("kivy")
if BOOT_DEBUG and _kv_mod is not None:
    _boot_file("kivy_client module imported ok (KIVY=%s KIVYMD=%s) "
               "kivy=%s" % (KIVY, KIVYMD, _kv_mod.__version__))

ATT_PLACEHOLDER_TEXT = "[loading image…]"

# G3 (UIX P3-4): guard the KV load. build() is called by app.run(),
# but test harnesses also call it directly; loading kv_md.kv twice
# makes Kivy warn "loaded multiples times" and re-declare every class.
# The flag makes a second build() a no-op for the KV load only.
_KV_LOADED = False


# Default UI palette — the single source of truth for theme colors. The app
# seeds theme_colors from this, the config screen's Reset restores it, and
# headless callers install it on a bare App so KV's app.theme_colors[...]
# expressions resolve (THEME_KEYS + THEME_LABELS drive the config screen's
# generated colour rows).
DEFAULT_THEME_COLORS = {
    "bg_main": (0.043, 0.063, 0.090, 1),       # #0B1017 midnight
    "panel": (0.067, 0.098, 0.145, 1),         # #111925 conversation
    "panel_darker": (0.075, 0.118, 0.169, 1),  # #131E2B composer
    "bg_sidebar": (0.055, 0.082, 0.122, 1),    # #0E151F navigation
    "sidebar_ctrl": (0.090, 0.133, 0.192, 1),  # #172231 selected surface
    "topbar": (0.075, 0.118, 0.169, 1),        # #131E2B header
    "primary": (0.961, 0.718, 0.373, 1),      # #F5B75F warm signal
    "accent": (0.345, 0.839, 0.765, 1),       # #58D6C3 live state
    "text": (0.957, 0.941, 0.906, 1),         # #F4F0E7 warm white
    "text_muted": (0.722, 0.769, 0.820, 1),   # #B8C4D1 secondary
    "text_dim": (0.569, 0.639, 0.706, 1),     # #91A3B4 metadata
    "input_bg": (0.090, 0.133, 0.192, 1),
    "input_bg_focus": (0.110, 0.173, 0.239, 1),
    "msg_own": (0.106, 0.173, 0.227, 1),      # #1B2C3A own posts
    "btn_text": (0.063, 0.090, 0.133, 1),
    "border": (0.388, 0.478, 0.529, 1),       # #637A87 visible outline
    "border_subtle": (0.165, 0.231, 0.286, 1),
    "aurora_a": (0.96, 0.72, 0.37, 0.22),     # amber orb (fork merge)
    "aurora_b": (0.35, 0.84, 0.77, 0.18),     # teal orb (fork merge)
    "aurora_c": (0.50, 0.40, 0.95, 0.20),     # violet orb (fork merge)
}

THEME_KEYS = (
    "bg_main", "panel", "panel_darker", "bg_sidebar", "sidebar_ctrl",
    "topbar", "primary", "accent", "text", "text_muted", "text_dim",
    "input_bg", "input_bg_focus", "msg_own", "btn_text", "border",
    "border_subtle",
    "aurora_a", "aurora_b", "aurora_c",
)

# Human-readable label for each THEME_KEYS entry, rendered by the config
# screen's generated colour rows (ConfigScreen._ensure_color_rows).
THEME_LABELS = {
    "bg_main": "Background",
    "panel": "Chat area",
    "panel_darker": "Input bar",
    "bg_sidebar": "Side windows",
    "sidebar_ctrl": "Side window controls",
    "topbar": "Top bar",
    "primary": "Primary button",
    "accent": "Accent",
    "text": "Body text",
    "text_muted": "Muted text",
    "text_dim": "Dim text",
    "input_bg": "Input fill",
    "input_bg_focus": "Input fill (focused)",
    "msg_own": "Own message bubble",
    "btn_text": "Button text",
    "border": "Borders / outlines",
    "border_subtle": "Dividers",
    "aurora_a": "Aurora amber",
    "aurora_b": "Aurora teal",
    "aurora_c": "Aurora violet",
}

DEBUG = False  # set via --debug CLI arg; enables verbose attachment/ingest logging


# ---------------------------------------------------------------------------
# Default community hub
# ---------------------------------------------------------------------------
# The official sHades community hub runs on this VPS. A fresh install (no
# saved session) connects here automatically instead of making the user
# type a server address on the connect screen.
#
#   199.231.189.136:29221 -> sHades guild server (TCP interface)
#   199.231.189.136:4242  -> rsReticulum community hub (transport node)
#
# The guild server is the one clients speak the sHades API to, so that is
# what the client points at. The rsReticulum instance on 4242 is a
# transport node that relays for other peers on the mesh.
DEFAULT_HUB_HOST = "199.231.189.136"
DEFAULT_HUB_PORT = 29221
DEFAULT_HUB_ADDRESS = "%s:%d" % (DEFAULT_HUB_HOST, DEFAULT_HUB_PORT)

# The guild's nomadnetwork.node hash. Left blank on purpose: the client
# discovers it from the guild's announce, which is what keeps working if
# the guild is ever rebuilt with a fresh identity.
DEFAULT_GUILD_NODE_HASH = ""


def default_username(user_hash):
    """A unique fallback display name for a user who has not chosen one.

    The guild derives unregistered users' names the same way (see
    sHades.get_username): "user-" + the first 6 hex chars of the
    lxmf.delivery hash, which is the guild's canonical user key. Using
    the identical convention here means a fresh client and the guild
    agree on the name, and no two clients can collide on it.

    A literal "user" would be taken by the first person to save it --
    the guild rejects a duplicate with 409 "username taken" -- and the
    name is UNIQUE across the whole guild, so it stays taken forever.
    """
    if not user_hash:
        return "user"
    return "user-" + str(user_hash)[:6]


def _free_client_port(preferred=29222):
    """Pick a free TCP listen port for the client's RNS server interface.

    H1: the client used to hardcode 29222; if another process held it,
    RNS's bind raised inside Reticulum.__init__, which calls
    RNS.panic() = os._exit(255) -- the app vanished with no error and
    no chance for the caller's try/except to run. Probe-bind the
    preferred port first; on EADDRINUSE walk upward (29222+N), and
    fall back to an OS-assigned port (bind 0) if the whole range is
    busy. A tiny TOCTOU gap remains (the port could be taken between
    the probe and RNS's bind) but the common case -- a second client
    instance, or a stale holder -- is covered.
    """
    import socket

    def _try(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if _try(preferred):
        return preferred
    for port in range(preferred + 1, preferred + 100):
        if _try(port):
            return port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _app_dir():
    """Directory holding the app's bundled resources (kv_md.kv, media/).

    Works from a source checkout and from a PyInstaller bundle, where data
    files are unpacked into sys._MEIPASS. Builder.load_file() and the media
    lookup both resolve relative to the CURRENT WORKING DIRECTORY otherwise,
    so a binary launched from any other directory (or a fresh install run
    from ~) would fail to find them.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(__file__))


def _register_ui_fonts():
    """Fork merge: font registration for the ported kv faces (MeshDisplay,
    MeshBody, MeshMono, MeshIcons). No-op without kivy; the fonts ride the
    fonts/ directory next to kv_md.kv."""
    if "kivy" not in sys.modules:
        return
    from kivy.core.text import LabelBase
    fonts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    body = os.path.join(fonts, "SourceSans3-400.ttf")
    body_bold = os.path.join(fonts, "SourceSans3-700.ttf")
    LabelBase.register(name="MeshBody", fn_regular=body, fn_bold=body_bold)
    # NOTE (fork merge decision): the fork also rebinds Roboto to the body
    # face. We deliberately do NOT: our connect-form geometry checks (and
    # several fixed-width controls) were designed against KivyMD's default
    # Roboto metrics, and rebinding shifts every built-in widget's width.
    LabelBase.register(name="MeshDisplay",
                       fn_regular=os.path.join(fonts, "SpaceGrotesk-400.ttf"),
                       fn_bold=os.path.join(fonts, "SpaceGrotesk-700.ttf"))
    LabelBase.register(name="MeshMono",
                       fn_regular=os.path.join(fonts, "IBMPlexMono-400.ttf"))
    LabelBase.register(name="MeshIcons",
                       fn_regular=os.path.join(fonts, "Phosphor.ttf"))


# ---------------------------------------------------------------------------
# API client over RNS link — GuildAPI lives in guild_api.py now
# (imported above; re-exported here so kc.GuildAPI keeps working).
# ---------------------------------------------------------------------------


IMAGE_CACHE = ImageCache()


# ---------------------------------------------------------------------------
# Widgets — moved to client_widgets.py (re-exported above).
# ---------------------------------------------------------------------------


if KIVY:
    class PfpImage(Image):
        """Avatar with a theme fallback.

        An Image with no texture draws its rect solid white, so every
        avatar-less user showed a stark white square (message rows,
        member rows, composer, profile, onboarding preview). While no
        texture is loaded the rect is hidden (color alpha 0) and a
        theme disc with the user's initial shows instead -- the same
        letter-tile pattern the server rail already uses. When a real
        avatar loads, the disc and letter hide and the texture shows.
        """
        fallback_letter = StringProperty("")

        def __init__(self, **kw):
            super().__init__(**kw)
            with self.canvas.before:
                self._fb_color = Color(
                    rgba=list(DEFAULT_THEME_COLORS["sidebar_ctrl"]))
                self._fb_disc = Ellipse(pos=self.pos, size=self.size)
            with self.canvas.after:
                # White: the CoreLabel texture already carries the
                # theme letter color; this must not tint it twice.
                self._fb_letter_color = Color(rgba=(1, 1, 1, 1))
                self._fb_letter_rect = Rectangle(texture=None,
                                                 pos=self.pos, size=(0, 0))
            self.bind(texture=self._sync_fallback,
                      size=self._sync_fallback, pos=self._sync_fallback,
                      fallback_letter=self._sync_fallback)
            app = App.get_running_app()
            if app is not None:
                try:
                    app.bind(theme_colors=self._sync_fallback)
                except Exception:
                    pass
            self._sync_fallback()

        def _fb_bg(self):
            app = App.get_running_app()
            if app is not None:
                try:
                    return list(app.theme_colors["sidebar_ctrl"])
                except Exception:
                    pass
            return list(DEFAULT_THEME_COLORS["sidebar_ctrl"])

        def _fb_fg(self):
            app = App.get_running_app()
            if app is not None:
                try:
                    return list(app.theme_colors["text_muted"])
                except Exception:
                    pass
            return list(DEFAULT_THEME_COLORS["text_muted"])

        def _sync_fallback(self, *args):
            self._fb_disc.pos = self.pos
            self._fb_disc.size = self.size
            if self.texture is not None:
                self.color = (1, 1, 1, 1)
                self._fb_color.a = 0
                self._fb_letter_color.a = 0
                self._fb_letter_rect.texture = None
                self._fb_letter_rect.size = (0, 0)
                return
            # No avatar: hide the white rect, show the disc + initial.
            self.color = (1, 1, 1, 0)
            self._fb_color.rgba = self._fb_bg()
            self._fb_color.a = 1
            letter = (self.fallback_letter or "")[:1]
            if not letter or min(self.width, self.height) <= 0:
                self._fb_letter_color.a = 0
                self._fb_letter_rect.texture = None
                self._fb_letter_rect.size = (0, 0)
                return
            from kivy.core.text import Label as CoreLabel
            label = CoreLabel(
                text=letter, bold=True,
                font_size=max(10, min(self.width, self.height) * 0.45),
                color=self._fb_fg())
            label.refresh()
            tex = label.texture
            self._fb_letter_rect.texture = tex
            self._fb_letter_rect.size = tex.size
            # Task 2: compute from pos/size, not from center -- center
            # is an AliasProperty whose cached value can still be stale
            # inside a size/pos dispatch (a layout-sized avatar, e.g.
            # the onboarding one with size_hint_y=1, synced its letter
            # to a transient center and the letter landed at the top
            # edge of the disc).
            x, y = self.pos
            w, h = self.size
            self._fb_letter_rect.pos = (
                x + (w - tex.width) / 2, y + (h - tex.height) / 2)
            self._fb_letter_color.a = 1

    class PassThroughImage(Image):
        """Image that passes touch events through to parent widget."""
        def on_touch_down(self, touch):
            return False

    class PFPEditorWidget(FloatLayout):
        """Round-mask avatar cropper — image at natural size (never stretched),
        circle overlay is draggable and resizable."""
        source = StringProperty("")
        on_save = ObjectProperty(None, allownone=True)
        circle_x = NumericProperty(0.5)   # relative center x (0-1)
        circle_y = NumericProperty(0.5)   # relative center y (0-1)
        circle_radius = NumericProperty(0.25)  # relative radius (0-1)
        # Computed pixel values for KV canvas drawing
        crop_cx = NumericProperty(0)
        crop_cy = NumericProperty(0)
        crop_r = NumericProperty(0)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._drag_mode = None   # 'move' or 'resize'
            self._last_pos = None
            self._img = None
            self.bind(
                circle_x=self._update_crop,
                circle_y=self._update_crop,
                circle_radius=self._update_crop,
                width=self._update_crop,
                height=self._update_crop,
                pos=self._update_crop,
            )
            self._update_crop()

        def _update_crop(self, *args):
            self.crop_cx = self.x + self.circle_x * self.width
            self.crop_cy = self.y + self.circle_y * self.height
            self.crop_r = self.circle_radius * min(self.width, self.height)
            self._size_image()

        def on_source(self, instance, value):
            """Re-size image when source changes or texture loads."""
            img = self.ids.get("pfp_img")
            if img:
                img.bind(texture_size=lambda *args: self._size_image())
            Clock.schedule_once(lambda dt: self._size_image(), 0)

        def _size_image(self):
            """Size the child image at natural size, centered, never stretched."""
            img = self.ids.get("pfp_img")
            if not img or not img.texture:
                return
            tw, th = img.texture_size
            if tw <= 0 or th <= 0:
                return
            if self.width <= 0 or self.height <= 0:
                return
            # Cap to fit within editor while preserving aspect ratio
            max_w = self.width * 0.95
            max_h = self.height * 0.95
            scale = min(max_w / tw, max_h / th, 1.0)
            img.size = (tw * scale, th * scale)
            img.pos = (
                self.center_x - img.size[0] / 2,
                self.center_y - img.size[1] / 2,
            )

        def _circle_hit(self, touch):
            dx = touch.x - self.crop_cx
            dy = touch.y - self.crop_cy
            return (dx * dx + dy * dy) ** 0.5

        def on_touch_down(self, touch):
            if not self.collide_point(*touch.pos):
                return super().on_touch_down(touch)
            r = self._circle_hit(touch)
            if r <= self.crop_r:
                self._drag_mode = 'move'
            else:
                self._drag_mode = 'resize'
            self._last_pos = touch.pos
            touch.grab(self)
            return True

        def on_touch_move(self, touch):
            if touch.grab_current is not self:
                return super().on_touch_move(touch)
            if self._last_pos is None:
                return True
            dx = touch.x - self._last_pos[0]
            dy = touch.y - self._last_pos[1]
            if self._drag_mode == 'move':
                new_cx = self.crop_cx + dx
                new_cy = self.crop_cy + dy
                r = self.crop_r
                new_cx = max(r, min(self.right - r, new_cx))
                new_cy = max(r, min(self.top - r, new_cy))
                self.circle_x = (new_cx - self.x) / self.width if self.width > 0 else 0.5
                self.circle_y = (new_cy - self.y) / self.height if self.height > 0 else 0.5
            elif self._drag_mode == 'resize':
                cx, cy = self.crop_cx, self.crop_cy
                new_dx = touch.x - cx
                new_dy = touch.y - cy
                new_r = (new_dx * new_dx + new_dy * new_dy) ** 0.5
                rel_r = max(0.05, min(new_r / min(self.width, self.height), 0.5))
                self.circle_radius = rel_r
                print(f"[PFP DEBUG] drag resize: circle_radius={self.circle_radius:.3f}")
            self._last_pos = touch.pos
            return True

        def on_touch_up(self, touch):
            if touch.grab_current is self:
                touch.ungrab(self)
                self._drag_mode = None
                self._last_pos = None
                return True
            return super().on_touch_up(touch)


    class FullScreenImageOverlay(FloatLayout):
        """Full-screen image viewer with dark overlay and download/close buttons."""
        source = StringProperty("")
        embed = ObjectProperty(None, allownone=True)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.size_hint = (1, 1)

        def download_image(self):
            """Save the displayed image to the user's Downloads folder.

            Sources are either local temp files (ephemeral embeds / cached
            avatars) or /file/attachments/... paths served by the guild over
            the RNS link. requests/HTTP is not used: it is not a declared
            dependency and none of these sources are HTTP URLs.
            """
            import threading
            import shutil
            from kivy.clock import Clock
            from kivy.app import App

            def do_download():
                try:
                    src = self.source
                    basename = os.path.basename(src.split("?")[0]) or "image.png"
                    out_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, basename)

                    if os.path.isfile(src):
                        # Local temp file (ephemeral embed or cached avatar).
                        shutil.copy2(src, out_path)
                    elif src.startswith("/file/"):
                        # Server-stored attachment — fetch via RNS link.
                        app = App.get_running_app()
                        api = getattr(app, "guild_api", None)
                        if api is None:
                            raise RuntimeError("Not connected to guild")
                        blob = api.fetch_file(src)
                        if not blob:
                            raise RuntimeError("Fetch returned empty")
                        with open(out_path, "wb") as f:
                            f.write(blob)
                    else:
                        raise RuntimeError("Unsupported source: %s" % src)

                    Clock.schedule_once(
                        lambda dt: self._show_status("Saved to %s" % out_path), 0)
                except Exception as e:
                    # Format the message NOW and close over the string: Python
                    # unbinds the `except ... as e` name when the block exits,
                    # so a lambda that referenced `e` would raise NameError when
                    # the Clock callback actually ran.
                    err = "Download failed: %s" % e
                    Clock.schedule_once(
                        lambda dt: self._show_status(err), 0)

            threading.Thread(target=do_download, daemon=True).start()

        def _show_status(self, text):
            app = App.get_running_app()
            if app:
                app._set_status(text)

        def dismiss(self):
            """Close the full-screen overlay and tell the embed to minimize."""
            app = App.get_running_app()
            if app and self.embed is not None:
                app.dismiss_fullscreen_image(self.embed)
            elif self.parent:
                self.parent.remove_widget(self)


    class CanvasWidget(Widget):
        """Simple widget that draws a colored circle on its canvas."""
        color = ListProperty([0.5, 0.5, 0.5, 1])  # default gray

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            with self.canvas:
                self._color_inst = Color(*self.color)
                self._ellipse = Ellipse(size=self.size, pos=self.pos)
            self.bind(pos=self._update_gfx, size=self._update_gfx, color=self._update_color)

        def _update_gfx(self, *args):
            self._ellipse.pos = self.pos
            self._ellipse.size = self.size

        def _update_color(self, instance, value):
            self._color_inst.rgba = value


    class MessageRow(ContextMenuMixin, BoxLayout):
        """One chat message."""
        author_hash = StringProperty("")
        username = StringProperty("user")
        ts = StringProperty("")
        content = StringProperty("")
        content_markup = StringProperty("")
        avatar_source = StringProperty("")
        msg_id = StringProperty("")
        is_own = BooleanProperty(False)
        # message grouping + reply cards (tier 2, drop parity)
        grouped = BooleanProperty(False)
        reply_author = StringProperty("")
        reply_excerpt = StringProperty("")

        def build_context_menu(self):
            app = App.get_running_app()
            actions = [
                ("copy text", bool(self.content),
                 lambda: app and app._copy_text(self.content or "")),
                ("reply", True,
                 lambda: app and app._reply_quote(self.username,
                                                  self.content or "")),
                ("react", True,
                 lambda: app and app._open_reaction_popup(self.msg_id)),
            ]
            if self.is_own and self.msg_id:
                actions.append(("edit", True,
                                lambda: app and app._open_edit_popup(
                                    self.msg_id, self.content or "")))
                actions.append(("delete", True,
                                lambda: app and app._delete_message(self.msg_id)))
            uh = self.author_hash or ""
            if app and uh and uh != app.my_hash and hasattr(app, "_friends"):
                actions.append(("message", app.lxmf_router is not None,
                                lambda: app and app._open_dm_popup(
                                    uh, self.username or "")))
                label = ("remove friend" if app._friends
                         and dms.is_friend(app._friends, uh)
                         else "add friend")
                actions.append((label, True,
                                lambda: app and app._toggle_friend(
                                    uh, self.username or "")))
            return actions

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.size_hint_y = None
            self.height = dp(56)
            self.padding = [dp(8), dp(4)]
            self.spacing = dp(8)
            # Ensure embed_container exists even if KV isn't loaded
            if not hasattr(self.ids, "embed_container"):
                from kivy.uix.boxlayout import BoxLayout
                container = BoxLayout(
                    orientation="vertical",
                    size_hint_y=None,
                    height=0,
                    spacing=dp(4),
                )
                container.bind(minimum_height=container.setter("height"))
                self.ids["embed_container"] = container
                self.add_widget(container)

        def on_content_markup(self, instance, value):
            # Rebuild content line after markup set
            pass


class CtxHeaderLabel(ContextMenuMixin, Label):
    """Guild header: right-click/long-press offers the invite share-link
    and (for admins) the server settings panel."""

    def build_context_menu(self):
        app = App.get_running_app()
        actions = [("copy invite link", True,
                    lambda: app and app._copy_invite())]
        if app and getattr(app, "i_am_admin", False):
            actions.append(("server settings", True,
                            lambda: app and app._open_admin_panel()))
        actions.append(("profile config", True,
                        lambda: app and app.open_config()))
        actions.append(("copy boot log", True,
                        lambda: app and app._copy_bootlog()))
        return actions


if KIVY:
    class GuildRailButton(ContextMenuMixin, ButtonBehavior, BoxLayout):
        """Rail disc for one server (task 6): first-letter tile until an icon
        path is ingested, active ring when selected, context menu with the
        server actions. Icon files arrive through _ingest_guild_icon (the
        whoami payload's blob id -> blob store -> local file)."""
        display_name = StringProperty("")
        icon_path = StringProperty("")
        active = BooleanProperty(False)
        index = NumericProperty(0)

        def __init__(self, **kw):
            super().__init__(**kw)
            self.orientation = "vertical"
            self.size_hint = (None, None)
            self.width = dp(56)
            self.height = dp(60)
            self.icon_widget = Image(size_hint=(None, None),
                                     width=dp(44), height=dp(44),
                                     allow_stretch=True, keep_ratio=True,
                                     pos_hint={"center_x": 0.5})
            self.letter_tile = Label(font_size=sp(20), bold=True,
                                     pos_hint={"center_x": 0.5})
            self.disc = Widget(size_hint=(None, None), size=(dp(48), dp(48)),
                               pos_hint={"center_x": 0.5, "center_y": 0.5})
            with self.disc.canvas.before:
                self._bg_color = Color(rgba=self._rail_bg())
                self._bg_ellipse = Ellipse(pos=(0, 0), size=(dp(48), dp(48)))
            self._inner = BoxLayout(size_hint_y=None, height=dp(48),
                                    padding=[dp(4)] * 2)
            self._inner.add_widget(self.icon_widget)
            self._inner.add_widget(self.letter_tile)
            self.add_widget(self._inner)
            self.bind(icon_path=self._apply_visual, active=self._apply_visual,
                      display_name=self._apply_visual,
                      size=self._sync_ellipse, pos=self._sync_ellipse)
            self._apply_visual()

        def _rail_bg(self):
            app = App.get_running_app()
            active = (app.theme_colors["primary"] if app
                      else (0.36, 0.41, 0.59, 1))
            idle = (app.theme_colors["sidebar_ctrl"] if app
                    else (0.24, 0.26, 0.3, 1))
            return list(active if self.active else idle) + [1]

        def _sync_ellipse(self, *a):
            x, y = self.disc.x, self.disc.y
            w, h = self.disc.width, self.disc.height
            self._bg_ellipse.pos = (x + dp(2), y + dp(2))
            self._bg_ellipse.size = (w - dp(4), h - dp(4))

        def _apply_visual(self, *a):
            app = App.get_running_app()
            self._bg_color.rgba = self._rail_bg()
            if self.icon_path and os.path.isfile(self.icon_path):
                self.icon_widget.source = self.icon_path
                self.icon_widget.opacity = 1
                self.letter_tile.opacity = 0
            else:
                self.icon_widget.source = ""
                self.icon_widget.opacity = 0
                self.letter_tile.text = (self.display_name or "S")[:1].upper()
                self.letter_tile.color = (
                    (1, 1, 1, 1) if self.active
                    else (app.theme_colors["text"] if app else (0.9, 0.9, 0.9, 1)))
                self.letter_tile.opacity = 1
            self.letter_tile.font_size = sp(18)

        def build_context_menu(self):
            """open / copy invite / server settings (admins) / remove server."""
            app = App.get_running_app()
            name = self.display_name or ("server %d" % self.index)
            actions = [("open " + name, True,
                        lambda: app and app._select_server(self.index))]
            if app and getattr(app, "guild_api", None):
                actions.append(("copy invite link", True,
                                lambda: app and app._copy_invite()))
            if app and getattr(app, "i_am_admin", False):
                actions.append(("server settings", True,
                                lambda: app and app._open_admin_panel()))
            actions.append(("profile config", True,
                            lambda: app and app.open_config()))
            actions.append(("copy boot log", True,
                            lambda: app and app._copy_bootlog()))
            actions.append(("remove server", True,
                            lambda: app and app._remove_server(self.index)))
            return actions


    class IconBase(ButtonBehavior, Widget):
        """Canvas-drawn top-bar icon. The default Kivy font (Roboto) has no
        glyphs for ☰/⌕/⚙ — they render as boxes with X's — so the icons are
        drawn, not typed. Geometry lives in ui_geom.py (Kivy-free, unit
        tested); this class only emits the instructions."""
        color = ListProperty([])

        def __init__(self, **kw):
            super().__init__(**kw)
            self.size_hint = (None, None)
            self.size = (dp(40), dp(40))
            # Default to theme text color if available
            if not self.color:
                app = App.get_running_app()
                self.color = app.theme_colors.get('text', (1, 1, 1, 1)) if app else (1, 1, 1, 1)
            self.bind(pos=self._redraw, size=self._redraw, color=self._redraw)
            Clock.schedule_once(lambda dt: self._redraw(), 0)

        def _redraw(self, *a):
            self.canvas.clear()
            with self.canvas:
                Color(*self.color)
                self.render_icon()

        def render_icon(self):  # pragma: no cover - abstract
            pass

    class HamburgerIcon(IconBase):
        """Sidebar toggle: three horizontal bars."""

        def render_icon(self):
            for px, py, bw, bh in ui_geom.hamburger_bars(
                    self.x, self.y, self.width, self.height):
                Rectangle(pos=(px, py), size=(bw, bh))

    class SearchIcon(IconBase):
        """Search: a circle outline with a handle."""

        def render_icon(self):
            cx, cy, r = ui_geom.search_circle(self.x, self.y,
                                              self.width, self.height)
            Line(circle=(cx, cy, r))
            Line(points=list(ui_geom.search_handle(self.x, self.y,
                                                   self.width, self.height)),
                 width=dp(1.5))

    class GearIcon(IconBase):
        """Settings: toothed ring around a hub dot."""

        def render_icon(self):
            cx = self.x + self.width / 2.0
            cy = self.y + self.height / 2.0
            r1, teeth = ui_geom.gear(cx, cy, min(self.width, self.height))
            Line(circle=(cx, cy, r1), width=dp(1.4))
            for x1, y1, x2, y2 in teeth:
                Line(points=[x1, y1, x2, y2], width=dp(1.4))
            Ellipse(pos=(cx - dp(2.5), cy - dp(2.5)), size=(dp(5), dp(5)))


# ---------------------------------------------------------------------------
# KV UI
# ---------------------------------------------------------------------------
# The KV rules live in kv_md.kv (loaded in build()). They used to be
# duplicated here as an inline KV string; keeping two copies meant edits
# landed in one file and not the other, which broke the app. Do not
# re-add an inline copy — edit kv_md.kv only.


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
if KIVY:
    class SiteEditorScreen(Screen):
        """P7c: micron source editor for a site page. The affordance
        reaches this screen only for SITE_EDIT holders (the server stamps
        site/get with can_edit). Holds the source TextInput, a one-tap
        preview, and publish via site/put; the app carries the flow."""

        def __init__(self, **kw):
            super().__init__(**kw)
            # Task 2: the screen had no background (transparent -- the
            # window's black showed through), default grey bar buttons
            # and a white default TextInput. Theme it like the rest of
            # the app, live-bound so a theme change re-tints it.
            with self.canvas.before:
                self._bg_color = Color(
                    rgba=list(DEFAULT_THEME_COLORS["bg_main"]))
                self._bg_rect = Rectangle(pos=self.pos, size=self.size)
            self.bind(size=self._update_bg, pos=self._update_bg)
            col = BoxLayout(orientation="vertical", padding=dp(8),
                            spacing=dp(6))
            bar = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
            self._bar_btns = []
            # Polish group 5: the bar buttons are FancyMDButtons, not
            # plain Buttons -- Publish is primary, Back/Preview
            # secondary. theme_width Custom + size_hint_x 1 keeps the
            # three buttons sharing the bar equally (a plain MDButton
            # auto-sizes to its text instead).
            from kivymd.uix.button import MDButtonText
            for text, meth, primary in (
                    ("< Back", "_site_editor_back", False),
                    ("Preview", "_site_editor_preview", False),
                    ("Publish", "_site_editor_publish", True)):
                btn = FancyMDButton(
                    variant="primary" if primary else "secondary",
                    theme_width="Custom", size_hint_x=1)
                # theme_text_color Custom or KivyMD's own rule
                # overwrites the assigned text_color with a theme value.
                label = MDButtonText(text=text, theme_text_color="Custom")
                btn.add_widget(label)
                btn._fancy_label = label
                # Plain .text attribute (not a FancyMDButton property:
                # the audio-row test pins the button itself textless).
                # The bar's tests read btn.text to find each button.
                btn.text = text
                if not primary:
                    # The bar keeps the sidebar_ctrl fill the sitenav
                    # tests pin; the secondary variant's panel base
                    # would change it. Override the base key, then
                    # repaint (construction already painted once).
                    btn._base_key = "sidebar_ctrl"
                    btn._apply_state()
                btn.bind(on_press=lambda inst, m=meth: self._tap(m))
                bar.add_widget(btn)
                self._bar_btns.append((btn, primary))
            self._src = TextInput(multiline=True, size_hint_y=0.5,
                                  font_size=sp(13),
                                  hint_text="micron source")
            self._status = Label(size_hint_y=None, height=dp(24),
                                 markup=True, halign="center",
                                 font_size=sp(12))
            self._status.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            prev_scroll = ScrollView(size_hint_y=1)
            prev_col = BoxLayout(orientation="vertical",
                                 size_hint_y=None,
                                 padding=(dp(4), dp(4)))
            prev_col.bind(minimum_height=prev_col.setter("height"))
            self._prev_holder = prev_col
            prev_scroll.add_widget(prev_col)
            col.add_widget(bar)
            col.add_widget(self._src)
            col.add_widget(self._status)
            col.add_widget(prev_scroll)
            self.add_widget(col)
            app = App.get_running_app()
            if app is not None:
                app.bind(theme_colors=self._apply_theme)
            self._apply_theme()

        def _update_bg(self, *args):
            self._bg_rect.pos = self.pos
            self._bg_rect.size = self.size

        def _apply_theme(self, *args):
            """Re-tint the editor after a theme change."""
            app = App.get_running_app()
            colors = (app.theme_colors if app is not None
                      else DEFAULT_THEME_COLORS)

            def get(key):
                try:
                    return colors.get(key, DEFAULT_THEME_COLORS[key])
                except Exception:
                    return DEFAULT_THEME_COLORS[key]

            self._bg_color.rgba = list(get("bg_main"))
            # Polish group 5: FancyMDButton owns its own bg (variant)
            # and re-tints itself on theme change; only the label text
            # colour still needs updating here.
            for btn, primary in self._bar_btns:
                btn._fancy_label.text_color = (get("btn_text") if primary
                                               else get("text"))
            self._src.background_color = get("input_bg")
            self._src.foreground_color = get("text")
            self._src.cursor_color = get("text")
            self._status.color = get("text_muted")
            self._update_bg()

        def _tap(self, meth):
            app = App.get_running_app()
            if app and hasattr(app, meth):
                getattr(app, meth)()

    class ConnectScreen(Screen):
        """Connect screen. The optional guild-hash fields live behind a
        disclosure so the screen fits a small window without pushing the
        Connect button off-screen."""

        def toggle_advanced(self):
            """Show/hide the optional node/delivery hash fields."""
            try:
                box = self.ids["advanced_box"]
                label = self.ids["advanced_label"]
            except Exception:
                return
            open_ = box.opacity > 0
            if open_:
                box.height = 0
                box.opacity = 0
                label.text = "Advanced: guild hashes"
            else:
                # minimum_height on the horizontal box is the tallest
                # child row; the two 44px fields sit in a 420px column so
                # the open height is 44 + 44 + spacing.
                box.height = box.minimum_height or dp(98)
                box.opacity = 1
                label.text = "Advanced: guild hashes (hide)"

    class OnboardingScreen(Screen):
        stage = NumericProperty(0)
        titles = ("Your identity", "Your guilds", "Your mesh")
        descriptions = (
            "Your keys live on this device. Your name and avatar travel with you.",
            "Gather in channels, open NomadNet sites and talk directly with friends.",
            "Messages can wait for a path. The app shows when the guild accepts them.",
        )

        def advance(self):
            if self.stage < 2:
                if App.get_running_app().reduced_motion:
                    self._next_card(self.ids.intro_card)
                    return
                from kivy.animation import Animation
                card = self.ids.intro_card
                Animation.cancel_all(card)
                fade = Animation(opacity=0, duration=0.12)
                fade.bind(on_complete=lambda *_: self._next_card(card))
                fade.start(card)
            else:
                name = (self.ids.welcome_name.text or "").strip()
                if not name:
                    # tay (2026-11): a username is required before the
                    # first connection; the hint stays until one is typed.
                    return

                def _staged(wid):
                    widget = self.ids.get(wid)
                    return ((widget.text or "").strip() if widget else "")

                app = App.get_running_app()
                # tay (2026-11): the staged screen carries the hub
                # destination alongside the profile; the sheet's connect
                # path then uses exactly what the stager typed.
                app._onboard_hub = (_staged("onb_hub"),
                                    _staged("onb_node"),
                                    _staged("onb_delivery"))
                app.open_recovery_sheet(onboarding_name=name)

        def _next_card(self, card):
            self.stage += 1
            from kivy.animation import Animation
            Animation(opacity=1, duration=0.28, t="out_cubic").start(card)

    class ProfileScreen(Screen):
        """Profile view: avatar, username, identity hashes, edit controls."""
        target_hash = StringProperty("")
        target_username = StringProperty("")
        target_avatar = StringProperty("")
        # The user's lxmf.delivery destination hash. Distinct from
        # target_hash (the user hash the guild keys members by).
        target_delivery = StringProperty("")

    class MemberRow(ContextMenuMixin, BoxLayout):
        username = StringProperty("")
        avatar_source = StringProperty("")
        author_hash = StringProperty("")
        presence_color = ListProperty([0.5, 0.5, 0.5, 1])  # gray = unknown/offline

        def build_context_menu(self):
            app = App.get_running_app()
            name = self.username or ""
            uh = self.author_hash or ""
            actions = [
                ("open profile", bool(uh),
                 lambda: app and app.open_profile(uh, name)),
                ("mention @%s" % name, bool(name),
                 lambda: app and app._mention(name)),
            ]
            if app and uh and uh != app.my_hash:
                actions.append(("message", app.lxmf_router is not None,
                                lambda: app and app._open_dm_popup(uh, name)))
                label = ("remove friend" if app._friends
                         and dms.is_friend(app._friends, uh)
                         else "add friend")
                actions.append((label, True,
                                lambda: app and app._toggle_friend(uh, name)))
            if app and app.i_am_moderator and uh and uh != app.my_hash:
                actions.append(("mute user", True,
                                lambda: app and app._mod_action("mod_mute", uh, name)))
                actions.append(("ban user", True,
                                lambda: app and app._mod_action("mod_ban", uh, name)))
            if app and app._can_manage_roles() and uh and uh != app.my_hash:
                actions.append(("roles...", True,
                                lambda: app and app._open_roles_popup(uh, name)))
            return actions

    class MainScreen(Screen):
        # Default: chat only. Both drawers closed (tay's direction: the
        # main app screen defaults to the chat; side panels open by
        # SWIPE, not by tap -- see on_touch_* below).
        left_open = BooleanProperty(False)
        right_open = BooleanProperty(False)
        # Animated drawer widths (polish group 3): KV binds each
        # sidebar's width to one of these, and _slide_drawer animates
        # it 0 <-> open width. The booleans above stay the source of
        # truth for touch gating and mutual exclusion.
        left_width = NumericProperty(0)
        right_width = NumericProperty(0)
        _current_channel = StringProperty("general")

        def __init__(self, **kw):
            super().__init__(**kw)
            self.bind(size=lambda *_: self._sync_drawer_widths())

        def _drawer_target(self, side):
            import ui_fx
            return ui_fx.drawer_open_width(self.width, side)

        def _slide_drawer(self, side, opened):
            """Slide one drawer to its open width (or back to 0).

            A mid-slide toggle cancels the running slide and restarts
            from the current width, so the boolean and the width can
            never disagree at rest.
            """
            import ui_fx
            prop = "left_width" if side == "left" else "right_width"
            target = self._drawer_target(side) if opened else 0
            anim = ui_fx.slide_property(self, prop, target)
            if anim is not None:
                anim.bind(on_complete=lambda *_: self._sync_drawer_widths())

        def _sync_drawer_widths(self, *_args):
            """Keep an open drawer's width right across resizes.

            The KV formula used to recompute from root.width; with the
            width animated, a resize has to land it instead. A drawer
            whose slide is running is skipped (the animation owns its
            width until it lands, then on_complete re-syncs).
            """
            import ui_fx
            for side, prop, flag in (("left", "left_width", self.left_open),
                                     ("right", "right_width",
                                      self.right_open)):
                if ui_fx.is_animating(self, prop):
                    continue
                target = self._drawer_target(side) if flag else 0
                setattr(self, prop, target)

        def toggle_left(self):
            self.left_open = not self.left_open

        def toggle_right(self):
            self.right_open = not self.right_open

        def on_left_open(self, instance, opened):
            if opened:
                self.right_open = False
                Clock.schedule_once(self._log_left_panel_width, 0.2)
            self._slide_drawer("left", opened)

        def _log_left_panel_width(self, *_dt):
            try:
                left = self.ids["left_sidebar"]
                _boot_file("SWIPE panel left_open=%s left.x=%s %s"
                           % (self.left_open, round(left.x, 1),
                              round(left.width, 1)))
            except Exception:
                _boot_file("SWIPE panel width log failed")

        def on_right_open(self, instance, opened):
            if opened:
                self.left_open = False
            self._slide_drawer("right", opened)

        # -- swipe gestures (edge bands + drawn-panel travel) ------------
        # Kivy routes on_touch_down to parent before children, so we only
        # CLASSIFY here (never consume): returning super()'s result lets
        # buttons and scrolls behave normally. A drag fires at most once.
        _SWIPE_EDGE = None   # "left_open" / "left_close" / right_*
        _SWIPE_FIRE = False

        def _swipe_plan(self, touch):
            """Classify a new touch into a drawer gesture or None.

            Left edge band (in from the screen's left) opens the channels
            drawer on rightward travel; with that drawer open, a band just
            inside its right border closes it on leftward travel. Mirrored
            for the members drawer on the right side.
            """
            try:
                left = self.ids["left_sidebar"]
                right = self.ids["right_sidebar"]
            except Exception:
                return None
            x, w = touch.x, self.width
            band = dp(24)
            if x <= band:
                return None if self.left_open else "left_open"
            if x >= w - band:
                return None if self.right_open else "right_open"
            # closing bands: inside an open drawer near its inner border
            if self.left_open and left.width > dp(0) and \
                    abs(x - (left.x + left.width)) <= band:
                return "left_close"
            if self.right_open and right.width > dp(0) and \
                    abs(x - right.x) <= band:
                return "right_close"
            return None

        def on_touch_down(self, touch):
            self._SWIPE_EDGE = self._swipe_plan(touch)
            self._SWIPE_FIRE = False
            self._swipe_x = touch.x
            _boot_file("SWIPE down x=%s edge=%s" % (
                round(touch.x, 1), self._SWIPE_EDGE))
            return super().on_touch_down(touch)

        def on_touch_move(self, touch):
            if self._SWIPE_EDGE and not self._SWIPE_FIRE and \
                    abs(touch.x - self._swipe_x) >= dp(24):
                _boot_file("SWIPE move x=%s dx=%s edge=%s" % (
                    round(touch.x, 1),
                    round(touch.x - self._swipe_x, 1),
                    self._SWIPE_EDGE))
            if self._SWIPE_EDGE and not self._SWIPE_FIRE and \
                    abs(touch.x - self._swipe_x) >= dp(48):
                self._SWIPE_FIRE = True
                opening = self._SWIPE_EDGE.endswith("_open")
                rightward = touch.x > self._swipe_x
                on_right = self._SWIPE_EDGE.startswith("right")
                # open = drag toward center; close = drag away
                if (rightward if not on_right else not rightward):
                    if opening:
                        target = ("right_open" if on_right else "left_open")
                        _boot_file("SWIPE firing %s=True (dir ok)" % target)
                        setattr(self, target, True)
                else:
                    closing_left = self._SWIPE_EDGE == "left_close"
                    closing_right = self._SWIPE_EDGE == "right_close"
                    if closing_left:
                        self.left_open = False
                    elif closing_right:
                        self.right_open = False
            return super().on_touch_move(touch)

        def on_touch_up(self, touch):
            if self._SWIPE_EDGE:
                _boot_file("SWIPE up x=%s fired=%s" % (
                    round(touch.x, 1), self._SWIPE_FIRE))
            self._SWIPE_EDGE = None
            self._SWIPE_FIRE = False
            return super().on_touch_up(touch)

    class ConfigScreen(Screen):
        """Config screen: unified UI theme colors, persisted to ui_config.json.

        The colour rows are generated in Python from THEME_KEYS
        (_ensure_color_rows) instead of hand-written KV blocks, so adding a
        token to DEFAULT_THEME_COLORS/THEME_KEYS cannot drift from the UI.
        """

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            # Field for each THEME_KEYS entry, filled by _ensure_color_rows.
            # Widgets added from Python are NOT registered in KV ids, so the
            # old c_<key> id convention is replaced by this dict.
            self._color_fields = {}
            self._color_labels = []
            self._voice_page = None

        def switch_tab(self, tab):
            """Profile config tabs (tay 2026-11-25): 'general' keeps the
            original appearance rows; 'voice' is the user settings
            submenu (tx mode, voc threshold, ptt key). Tabs swap the
            scroll content by widget add/remove, so the minimum-height
            KV bindings never fight a python height override."""
            try:
                tb = self.ids["tab_general"]
                tv = self.ids["tab_voice"]
                host = self.ids["config_host"]
                pages = self.ids["config_pages"]
                foot = self.ids.get("config_footer")
                general = tab != "voice"
                tb.variant = "primary" if general else "outlined"
                tv.variant = "primary" if not general else "outlined"
                host.remove_widget(pages)
                if self._voice_page is not None:
                    self._voice_page.stop_capture()
                    host.remove_widget(self._voice_page)
                if general:
                    host.add_widget(pages)
                    if foot is not None:
                        foot.height = dp(56)
                        foot.opacity = 1
                else:
                    self._ensure_voice_page()
                    if foot is not None:
                        foot.height = 0
                        foot.opacity = 0
            except Exception as e:
                print("profile-config tab switch failed:", repr(e))

        def _ensure_voice_page(self):
            """Build the user settings page (voice rows) once; it saves
            instantly, so no footer applies to it. The page hosts
            directly in the scroll host: the parked voice_host container
            sat inside config_pages (height 0, opacity 0), which
            switch_tab had just removed -- adding there rendered an
            empty tab on every platform (tay 2026-11-26 photo)."""
            if self._voice_page is not None:
                return
            try:
                from ui_user_settings import VoiceSettingsPage
                host = self.ids["config_host"]
                page = VoiceSettingsPage(
                    client=App.get_running_app())
                host.add_widget(page)
                self._voice_page = page
            except Exception as e:
                print("voice settings page unavailable:", repr(e))

        def _ensure_color_rows(self):
            """Build one Label+MDTextField row per THEME_KEYS entry (once)."""
            if self._color_fields:
                return
            try:
                grid = self.ids["theme_rows"]
            except Exception:
                return
            from kivymd.uix.textfield import MDTextField
            from kivy.app import App as _App
            app = _App.get_running_app()
            for key in THEME_KEYS:
                # G6: rows are dp(60), not dp(44) -- a filled MDTextField
                # floors its own height at dp(56) (adjust_height), so a
                # 44px row let each field stick 12px out of its row and
                # the fields merged into one continuous block.
                row = BoxLayout(size_hint_y=None, height=dp(60),
                                padding=[0, dp(2)])
                lbl = Label(text=THEME_LABELS.get(key, key),
                            size_hint_x=0.34)
                fld = MDTextField(text="", mode="filled",
                                  size_hint_x=0.66)
                row.add_widget(lbl)
                row.add_widget(fld)
                self._color_fields[key] = fld
                self._color_labels.append(lbl)
                grid.add_widget(row)
            if app is not None:
                app.bind(theme_colors=self._apply_row_colors)
                self._apply_row_colors(app, app.theme_colors)

        def _apply_row_colors(self, app, colors):
            """Re-tint the generated rows after a theme change."""
            text = colors.get("text", (1, 1, 1, 1))
            for lbl in self._color_labels:
                lbl.color = text
            for fld in self._color_fields.values():
                fld.fill_color_normal = colors.get(
                    "input_bg", (0.09, 0.09, 0.09, 1))
                fld.fill_color_focus = colors.get(
                    "input_bg_focus", (0.11, 0.11, 0.11, 1))
                fld.text_color_normal = text
                fld.text_color_focus = text
                fld.cursor_color = text

        def on_pre_enter(self):
            """Build the rows (first entry) and prefill from the theme."""
            self._ensure_color_rows()
            self._prefill_fields()
            try:
                self.ids["config_status"].text = ""
            except Exception:
                pass
            # predictable entry point: appearance tab first
            self.switch_tab("general")

        def _prefill_fields(self):
            from kivy.app import App as _App
            app = _App.get_running_app()
            if app is None:
                return
            for key, fld in self._color_fields.items():
                try:
                    fld.text = rgba_to_hex(
                        app.theme_colors.get(key, (1, 1, 1, 1)))
                except Exception:
                    pass

        def save_config(self):
            """Parse the hex fields, apply the theme and persist it."""
            from kivy.app import App as _App
            app = _App.get_running_app()
            status = ""
            try:
                status = self.ids["config_status"]
            except Exception:
                status = None
            # Validate the whole form before changing the live palette.
            updated = dict(app.theme_colors)
            for key in THEME_KEYS:
                fld = self._color_fields.get(key)
                if fld is None:
                    continue
                try:
                    rgba = hex_to_rgba(fld.text)
                    # G4 (UIX P3-2): the hex fields carry no alpha, and
                    # hex_to_rgba returns 1.0 -- so restore the key's
                    # live alpha (the aurora orbs ship at 0.18-0.22 and
                    # a plain Save used to blow them out to opaque).
                    orig = app.theme_colors.get(key, (1, 1, 1, 1))
                    if len(orig) == 4:
                        rgba = (rgba[0], rgba[1], rgba[2], orig[3])
                    updated[key] = rgba
                except Exception as e:
                    if status:
                        status.text = "Invalid color for %s: %s" % (key, e)
                    return
            # Reassign the whole property so KV bindings re-evaluate every
            # app.theme_colors['key'] expression (item mutation alone does not
            # dispatch the property change).
            app.theme_colors = updated
            app._save_ui_config()
            if status:
                status.text = "Theme saved."

        def reset_config(self):
            from kivy.app import App as _App
            app = _App.get_running_app()
            app.theme_colors = dict(DEFAULT_THEME_COLORS)
            app._save_ui_config()
            # Reflect the restored theme in the fields immediately, not just
            # on the next entry into the screen.
            self._prefill_fields()
            try:
                self.ids["config_status"].text = "Theme reset to defaults."
            except Exception:
                pass

        def choose_preset(self, name):
            """Apply a named palette from ui_theme.PRESETS.

            Seeded from DEFAULT_THEME_COLORS (not the current per-key edits),
            matching the reference client: a preset is a clean re-tint, and
            the fine-tune rows then hold that palette for further edits.
            """
            from kivy.app import App as _App
            app = _App.get_running_app()
            if app is None:
                return
            try:
                app.theme_colors = preset_colors(
                    dict(DEFAULT_THEME_COLORS), name)
            except ValueError:
                status = self.ids.get("config_status")
                if status:
                    status.text = "Unknown preset."
                return
            app._save_ui_config()
            self._prefill_fields()
            status = self.ids.get("config_status")
            if status:
                status.text = "%s preset applied." % name


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
def _fail_validation(app, text):
    """Validation rejection: show it where the user can see it.

    H5: _set_status writes to the connect/profile screens' status
    label only. On a fresh install the app sits on the onboarding
    screen, so a rejection was invisible. Switch to the connect
    screen first, then write the status. Module-level (not a method)
    so unbound connect_to_guild callers with a minimal stub -- which
    only defines _set_status -- keep working.
    """
    try:
        app.root.current = "connect"
    except Exception:
        pass
    app._set_status(text)


class sHadesClientApp(SettingsMixin, ChannelsMixin, SiteMixin,
                      VoicePanelMixin, StreamMixin, MenusMixin,
                      AttachmentsMixin, MDApp):
    title = "sHades"

    guild_api = ObjectProperty(None, allownone=True)
    reticulum = ObjectProperty(None, allownone=True)
    link = ObjectProperty(None, allownone=True)
    # Unified UI palette: edited in the config screen, persisted to
    # ui_config.json, referenced by kv_md.kv as app.theme_colors['key'].
    theme_colors = DictProperty({})
    # sitenav (d/g): persistent site nav bar state, bound by kv_md.kv.
    # visible = a site page is on screen; path = "#channel /path" crumb;
    # can_back/can_forward gate the bar's Back/Forward controls.
    site_nav_visible = BooleanProperty(False)
    site_nav_path = StringProperty("")
    site_nav_can_back = BooleanProperty(False)
    site_nav_can_forward = BooleanProperty(False)
    # polish (g): composer upload progress bar state, bound by kv_md.kv.
    # upload_active shows the 4dp strip above the composer; upload_pct
    # (0-100) is the fill. Driven from _send_with_attachment's pstage
    # (via Clock, worker thread) and cleared by _attach_composer_stage("")
    # on every exit path of the send flow.
    upload_pct = NumericProperty(0)
    upload_active = BooleanProperty(False)
    # P5: sidebar banner strip (above the channel list); "" = letter tile
    banner_channel_path = StringProperty("")
    # P5: config-pane header strip (stays empty; bindings tolerate "")
    banner_config_path = StringProperty("")
    # P5: letter tile shown inside the banner strip when unset
    banner_letter = StringProperty("S")
    # P6: the server's message-of-the-day; "" = the strip stays hidden
    server_motd = StringProperty("")
    # P11 arrow 5: "0.36.22"-shaped hint from the server's whoami; ""
    # hides the strip (the update check is toggle-gated server-side)
    client_update_version = StringProperty("")
    update_status = StringProperty("")
    # P8: the composer's truth per channel (announce channels hide it
    # without an explicit grant; holders see the Announce send label)
    composer_readonly = BooleanProperty(False)
    composer_send_label = StringProperty("Send")
    # tay (2026-11): the onboarding avatar slot and the connect-screen
    # username gate bind to these; preview mirrors my_avatar_small so
    # the onboarding card shows the pick (or the generated pattern)
    # live.
    avatar_preview = StringProperty("")
    connect_username_required = BooleanProperty(True)
    # Task 2: username is a StringProperty (was a plain attribute) so
    # KV bindings on app.username stay live -- the composer avatar's
    # fallback letter and the connect screen's username field both
    # read it, and both were stale at the provisional "user" value.
    username = StringProperty("user")
    # path-state chip: the observed RNS path to the guild node
    # (offline / direct / relay + hop count), rendered by MeshPulse,
    # MeshConstellation and the header label in kv_md.kv
    mesh_mode = StringProperty("offline")
    mesh_hops = NumericProperty(0)
    # fork merge (recovery/onboarding): motion preference +
    # staged onboarding name
    reduced_motion = BooleanProperty(False)
    onboarding_name = StringProperty("")
    mesh_label = StringProperty("OFFLINE")
    # reply staging: the composer's "Replying to @x" card + wire prefix
    reply_draft = StringProperty("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Theme config - KivyMD 2.0.0 Material You color system
        # Use valid palette names: "Blue", "Purple", "Red", "Green", etc.
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Blue"
        self.theme_cls.accent_palette = "Orange"
        self.node_hash = None
        # The GUILD's nomadnetwork.node hash. Distinct from self.node_hash,
        # which is later reused for THIS client's own node destination.
        self.guild_node_hash = None
        self.delivery_hash = None
        self.client_identity = None
        self.client_dest = None
        # Provisional display name; replaced with a unique
        # "user-<hash6>" once the client identity exists (see
        # _init_identity_and_lxmf). Until then no guild call can be
        # made anyway -- every API call needs an identified link.
        self.username = "user"
        # Contact-card alias. No setter exists yet; the card payload
        # falls back to username. Initialised here, same law as above:
        # reads fire before any session exists (device receipt, 184a4f9:
        # AttributeError 'display_name' in _contact_card_payload the
        # moment an identity was live).
        self.display_name = ""
        self.my_hash = None
        # Which guild we are pointed at. These are plain attributes (not
        # properties), so they MUST be initialised here: on a fresh install
        # no session has been loaded yet and any read of them before the
        # first connect would raise AttributeError. Seeded with the default
        # community hub so a fresh install already has a valid target.
        self.hub_address = DEFAULT_HUB_ADDRESS
        self.hub_host = DEFAULT_HUB_HOST
        self.hub_port = DEFAULT_HUB_PORT
        self._seq = 0
        self._last_ts = 0.0
        self._channels = OrderedDict()   # name -> cid (filled on guild load)
        self._members = {}               # member_hash -> username
        self._msg_widgets = {}           # msg_id -> MessageRow
        self._member_widgets = {}        # user_hash -> MemberRow (sidebar)
        self._popup = None
        self._poll_error_shown = False
        self._ui_populated = False
        # v0.29: presence needs no heartbeat. The guild derives it from link
        # liveness (RNS keepalives + teardown), so nothing pings; register_node
        # stays a ONE-SHOT directory write at connect (and on reconnect).
        # v1 stream: long-polls ONLY the visible channel. _stream_cid is the
        # channel the guild should stream to us; setting it (with a None
        # cursor) makes the stream loop resubscribe — one subscription per
        # link is a server-side rule, so switching channels reroutes the flow.
        self._stream_cid = None
        self._stream_cursor = None
        self._stream_tick_event = None
        # site channels: kind per cid, the rendered page stack, the page
        # currently shown, and the active theme from the manifest
        self._channel_kinds = {}     # cid -> chat|site|term|voice|announce
        self._voice_state_init()     # item 6: voice rows + join panel
        self._channel_can_post = {}  # cid -> can_post (P8)
        self._channel_pos = {}       # name -> position (task 7 ordering)
        self._channel_order = {}     # cid -> position (server-side index)
        self._site_stack = []        # [(cid, path, scroll_y)] back stack
        self._site_forward = []      # [(cid, path, scroll_y)] forward stack
        self._site_current = None    # (cid, path)
        self._site_pending_scroll = 1.0  # scroll_y the next render restores
        self._site_theme = {}
        # term channels: one console session per channel, the live TermPage
        self._term_sessions = {}     # cid -> session id
        self._term_page = None       # the TermPage currently on screen
        # unread badges per cid (from channels/list receipts)
        self._channel_unread = {}
        # context menu state + moderator flag (from /api/v1/whoami)
        self._ctx_menu = None
        self.i_am_moderator = False
        # U5/U8: admin flag + role list, filled from /api/v1/whoami
        self.i_am_admin = False
        self.my_roles = []
        self.PERM_VIEW = 1
        self.PERM_POST = 2
        self.PERM_MANAGE_MESSAGES = 4
        self.PERM_MANAGE_CHANNEL = 8
        self.PERM_MANAGE_ROLES = 16
        self.PERM_ADMIN = 32
        # client-muted channels (persisted; suppresses unread badges only)
        self._muted_channels = set()
        try:
            cfg = os.path.join(_app_data_dir(), "muted_channels.json")
            if os.path.isfile(cfg):
                with open(cfg) as f:
                    self._muted_channels = set(json.load(f))
        except Exception as e:
            print("muted-channel load failed:", repr(e))

        # Presence: members arrive as a guild-wide PUSH event on the link
        # (server "presence" kind); _active_users maps user_hash ->
        # {"username", "node_hash"} and is rebuilt from each pushed event,
        # so a member who stops pinging drops out of the member list
        # instead of lingering in it. No interval fetches.
        self._active_users = {}
        # Has the guild answered a presence request? Distinguishes "nobody is
        # online" from "this guild has no presence API" — different UI each.
        self._presence_ok = False

        # Unified UI palette (single source of truth; edited in the config
        # screen, persisted to ui_config.json, referenced by kv as
        # app.theme_colors['key']). Seeded from DEFAULT_THEME_COLORS, then
        # overlaid with whatever the user saved.
        self.theme_colors = dict(DEFAULT_THEME_COLORS)
        self._load_ui_config()

        # Chat pagination: per channel, oldest loaded message id + whether
        # older messages remain. Used by scroll-triggered loading.
        self._older = {}          # cid -> {oldest_id, has_older, loading}
        # fork merge (offline cache): pending rows queue on the UI clock,
        # writes flush in a thread; purge/forgotten state keeps erased
        # identities from re-entering the store.
        self._cache_write_lock = threading.RLock()
        self._cache_pending = {}
        self._cache_event = None
        self._purged_before = {}
        self._forgotten_cache_guilds = set()
        self._pending_reply = None
        self._scroll_bound = False

        # Banner images - random selection from media folder on startup.
        self.banner_config_path = ""
        self.onboarding_name = "wanderer-%04x" % random.getrandbits(16)
        self.banner_channel_path = ""
        self._select_random_banners()

        # Avatar pfp paths (set by _save_pfp_crop)
        self.my_avatar_orig = None
        self.my_avatar_small = None
        # task 9: the composer's pfp live-binds to the kv bar; the class
        # property is declared next to the other app properties
        self.composer_pfp_path = ""

        # Friends roster + DM state (U7, client-local; the guild never sees
        # either). DM transport is a direct LXMRouter exchange, initialised
        # in _connect_after_rns (degrades gracefully if LXMF is missing).
        self._friends_path = os.path.join(_app_data_dir(), "friends.json")
        self._friends = dms.load_friends(self._friends_path)

        # Server/guild list (client-local; each entry has hub_address, node_hash,
        # delivery_hash, username, display_name, avatar_path). Only one server
        # is active at a time; channel list inherits from the active server.
        self._servers_path = os.path.join(_app_data_dir(), "servers.json")
        self._servers = self._load_servers()
        self._active_server_idx = 0 if self._servers else -1
        self.lxmf_router = None
        self.lxmf_delivery = None
        self._dm_popup = None
        self._dm_popup_peer = None
        self._dm_popup_grid = None
        self._dm_popup_input = None
        self._admin_popup = None

    # -- UI theme / config ----------------------------------------------

    # -- lifecycle -------------------------------------------------------
    def build(self):
        global _KV_LOADED
        if BOOT_DEBUG:
            _boot_mark("0.3 build(): python + kivy window alive")
        if Window:
            self.theme_cls.set_colors()
            Window.clearcolor = self.theme_cls.backgroundColor
        # Import KivyMD widgets so they're registered in Factory before KV loads
        from kivymd.uix.button import MDButton, MDButtonText, MDIconButton  # noqa: F401
        from kivymd.uix.textfield import MDTextField  # noqa: F401
        from kivymd.uix.appbar import MDTopAppBar  # noqa: F401
        from kivy.lang import Builder
        _register_ui_fonts()
        if not _KV_LOADED:
            Builder.load_file(os.path.join(_app_dir(), "kv_md.kv"))
            _KV_LOADED = True
        sm = ScreenManager()
        # Polish group 1: one short fade for every screen switch (connect,
        # onboarding, main, profile, config, site editor, server config),
        # same language as ui_fx.reveal(). ui_fx owns the duration and
        # retimes it to instant while reduced motion is on.
        import ui_fx
        sm.transition = FadeTransition(
            duration=ui_fx.SCREEN_TRANSITION_DURATION)
        ui_fx.register_screen_manager(sm)
        sm.add_widget(ConnectScreen(name="connect"))
        sm.add_widget(OnboardingScreen(name="onboarding"))
        sm.add_widget(MainScreen(name="main"))
        sm.add_widget(ProfileScreen(name="profile"))
        sm.add_widget(ConfigScreen(name="config"))
        sm.add_widget(SiteEditorScreen(name="site_editor"))
        # Phase 1 task 5: the tabbed server-config screen replaces the U8
        # popup; its tabs are keyed to the caller's roles on entry.
        try:
            from ui_server_config import ServerConfigScreen
            sm.add_widget(ServerConfigScreen(name="server_config"))
        except Exception as e:
            print("server_config screen unavailable:", repr(e))
        # Plain Enter sends the message; Shift+Enter inserts a newline
        # (multiline input). Handled via _on_key_down below.
        Window.bind(on_key_down=self._on_key_down)
        if BOOT_DEBUG:
            _boot_mark("screens ok: %d registered" % len(sm.screens))
            _boot_install(sm.get_screen(sm.current))
        return sm

    def _softinput_tick(self, *_dt):
        """Lift the composer group above the Android IME.

        tay's 0.36.13 boot (launch 1795165828s): the window-mode
        dispatch legs are green, but the ANGLE/Vulkan surface never
        propagates the IME resize into the kivy layout. v0.36.14 tried
        a UI-thread OnGlobalLayoutListener and KILLED the boot (log 30,
        launches 1795169468/71s: UI-thread python collided with the
        main thread mid-import of kivy's pyx machinery -- system kill,
        silence). v0.36.15 measures here, on the KIVY thread, post
        app start -- the exact pattern _apply_system_gesture_exclusions
        already runs: PythonActivity + Rect resolved once, then each
        0.25s tick reads the decor's visible frame bottom directly.
        lift = base - bottom is the IME height in pixels; padding the
        bottom edge of the chat container lifts the reply row and the
        composer row (input field, buttons, pfp) as one group while the
        scroll view shrinks. Zero lift when the keyboard is closed.
        Runs only on android.
        """
        from kivy.utils import platform
        if platform != "android":
            return
        global _FRAME_METER
        try:
            if _FRAME_METER is None:
                from jnius import autoclass
                _FRAME_METER = {
                    "PythonActivity": autoclass(
                        "org.kivy.android.PythonActivity"),
                    "Rect": autoclass("android.graphics.Rect"),
                }
                print("SOFTINPUT: frame meter live (kivy thread poll)",
                      flush=True)
            meter = _FRAME_METER
            rect = meter["Rect"]()
            meter["PythonActivity"].mActivity.getWindow().getDecorView() \
                .getWindowVisibleDisplayFrame(rect)
            bottom = rect.bottom
        except Exception as e:
            if not _softinput_reported_once("frame read"):
                _boot_file("softinput frame read failed: %s" % e)
            return
        try:
            import main as main_mod
            state = main_mod._SOFTINPUT
        except Exception:
            state = None
        if state is None:
            return
        # base = the no-IME frame bottom = the largest bottom seen; if
        # the IME was already up at first layout the base self-corrects
        # on the first keyboard close.
        if bottom and bottom > state["base"]:
            state["base"] = bottom
        state["bottom"] = bottom
        state["live"] = True
        base = state["base"]
        if not base:
            return
        lift = (base - bottom) if bottom else 0
        if lift < 0:
            lift = 0
        screen = self.root.get_screen("main")
        cs = screen.ids.get("chat_screen")
        if cs is None:
            return
        cur = cs.padding
        if len(cur) != 4 or cur[3] != lift:
            # kivy padding is [left, top, right, bottom]
            cs.padding = [cur[0], cur[1], cur[2], lift]

    def _apply_system_gesture_exclusions(self, *_args):
        """Exclude our drawer edge bands from the Android system back
        gesture (Android 10+).

        Without this, the OS intercepts swipes that start on the outer
        ~24dp of the screen, and the app never sees them: the left
        (server/channels) drawer becomes unreachable because it has no
        tap affordance by design, and the right members drawer loses its
        swipe too. The system honours at most 200dp of exclusion
        height per edge; we request the full screen height and let it
        cap, which still covers any practical swipe. No-op on desktop.
        """
        from kivy.utils import platform
        if platform != "android":
            return
        try:
            from jnius import autoclass
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Rect = autoclass("android.graphics.Rect")
            Rects = autoclass("java.util.ArrayList")
            activity = PythonActivity.mActivity
            decor = activity.getWindow().getDecorView()
            metrics = activity.getResources().getDisplayMetrics()
            band_px = int(dp(24) + dp(8))
            metrics = activity.getResources().getDisplayMetrics()
            w_px = int(metrics.widthPixels)
            h_px = int(metrics.heightPixels)
            left = Rect(0, 0, band_px, h_px)
            right = Rect(w_px - band_px, 0, w_px, h_px)
            rects = Rects()
            rects.add(left)
            rects.add(right)
        except Exception as e:
            _boot_file("GESTURE unavailable: %s" % e)
            return

        def _set(*_dt):
            try:
                decor.setSystemGestureExclusionRects(rects)
            except Exception as e:
                _boot_file("GESTURE failed: %s" % e)
                return
            _boot_file("GESTURE exclusions set: band %dpx "
                       "%dx%d" % (band_px, w_px, h_px))
        Clock.schedule_once(_set, 0.5)
        Clock.schedule_once(_set, 2.5)

    def on_start(self):
        if BOOT_DEBUG:
            _boot_mark("on_start")
        self._apply_system_gesture_exclusions()
        # 0.36.14: the frame meter (main.py, UI-thread listener) feeds
        # this tick; it pads the chat container so the composer group
        # rides above the keyboard.
        try:
            Clock.schedule_interval(self._softinput_tick, 0.25)
        except Exception as e:
            _boot_file("softinput meter not scheduled: %s" % e)
        # tay (2026-11): '/' pops the command palette for live selections.
        # The channel input watches its own text: a bare '/' opens the
        # palette with the filter seeded from the input, everything else
        # keeps the existing send path (kivy_client._handle_slash).
        try:
            _main = self.root.get_screen("main")
            _msg = _main.ids.get("msg_input")
            if _msg:
                _msg.bind(text=self._on_live_slash)
        except Exception as _exc:
            _boot_file("slash-live bind unavailable: %s" % _exc)
        # Try to resume a saved session
        sess = self._load_session()
        # fork merge: the identity file is the sole carrier of
        # the member's key. If a session or onboarding record
        # exists but the key file is gone, require a restore
        # instead of silently minting a new identity.
        if (sess or os.path.isfile(self._onboarding_path())) and not os.path.isfile(
                self._identity_file()):
            self.root.current = "connect"
            self._set_status("Identity missing. Restore your recovery kit or start fresh.")
            return
        if sess and sess.get("hub_address"):
            self.guild_node_hash = sess.get("node_hash")
            self.node_hash = self.guild_node_hash
            self.delivery_hash = sess.get("delivery_hash")
            # A session written by an older build may hold the literal
            # "user". Treat that as "no name chosen" so it is upgraded
            # to the unique default once the identity is built.
            saved_username = sess.get("username", "") or ""
            self.username = ("" if saved_username == "user" else saved_username)
            self.hub_address = sess.get("hub_address")
            self.hub_host = sess.get("hub_host")
            self.hub_port = sess.get("hub_port")
            # Try to match the session to a saved server
            if self.hub_host and self.hub_port:
                for idx, s in enumerate(self._servers):
                    if s.get("host") == self.hub_host and s.get("port") == self.hub_port:
                        self._active_server_idx = idx
                        break
            self._open_main()
            if BOOT_DEBUG:
                _boot_mark("session resumed: %s" % (self.hub_address,))
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0.5)
            # Re-render persisted history (embeds survive close/re-open) once
            # the guild API is up; server-stored attachments re-fetch with a
            # loading placeholder until their bytes arrive.
            Clock.schedule_once(self._load_history_ui, 2.0)
        else:
            # Fresh install: tay (2026-11), profile staging must be the
            # very first screen presented when no extant profile is
            # saved. The staged screen carries the hub destination and
            # guild-hash fields and replaces the standalone connect
            # screen for this path; completing it mints the profile and
            # then connects. A restored or previously completed profile
            # keeps the auto-connect fallthrough so upgrades never see
            # staging twice.
            if os.path.isfile(self._portable_profile_path()):
                if BOOT_DEBUG:
                    _boot_mark("profile exists -> default hub")
                self._auto_connect_default_hub()
            else:
                if BOOT_DEBUG:
                    _boot_mark("fresh install -> profile staging first")
                self.root.get_screen("onboarding").stage = 0
                self.root.current = "onboarding"
                self._set_status("Stage your profile: name, photo, hub address.")

    def _onboarding_path(self):
        return os.path.join(_app_data_dir(), "onboarding.json")

    def _portable_profile_path(self):
        return os.path.join(_app_data_dir(), "portable_profile.json")

    def _load_portable_profile(self):
        try:
            with open(self._portable_profile_path()) as source:
                data = json.load(source)
            if isinstance(data.get("name"), str):
                self._portable_name = data["name"][:24]
                self.username = self._portable_name or self.username
            avatar = data.get("avatar")
            if isinstance(avatar, str) and avatar.startswith(_app_data_dir()) \
                    and os.path.isfile(avatar):
                self.my_avatar_small = avatar
                self.avatar_preview = avatar
        except (OSError, ValueError, AttributeError):
            pass

    def _save_portable_profile(self):
        path = self._portable_profile_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w") as stream:
            json.dump({"name": self._portable_name,
                       "avatar": getattr(self, "my_avatar_small", None)}, stream)
        os.replace(temp, path)

    def complete_onboarding(self, name, hub_data=None):
        """Create a durable local identity before the first connection.

        tay (2026-11): a username is required before the first hub
        connection -- the staged "wanderer-hex" name is only a prefilled
        hint; if the user clears it the flow stops before identity
        creation rather than silently minting a name.
        """
        cleaned = re.sub(r"[^a-zA-Z0-9_. -]", "", name or "").strip()[:24]
        if not cleaned:
            self._set_status("A username is required before connecting.")
            self.root.current = "onboarding"
            return
        self.username = cleaned
        self._portable_name = self.username
        os.makedirs(_app_data_dir(), exist_ok=True)
        try:
            self._ensure_identity_file()
        except Exception as exc:
            self._set_status("Identity creation failed: %s" % exc)
            return
        if not (getattr(self, "my_avatar_small", None)
                and os.path.isfile(str(self.my_avatar_small))):
            # tay (2026-11): cheaply generated from the username when the
            # user did not pick a photo during onboarding.
            try:
                self._create_generated_avatar(self.username)
            except (OSError, ValueError) as e:
                print("avatar preview unavailable:", repr(e))
        self._save_portable_profile()
        with open(self._onboarding_path(), "w", encoding="utf-8") as f:
            json.dump({"name": self.username, "completed": True}, f)
        # card slice 2 (tay-approved): the moment the keyset exists, the
        # user gets their collectible card - the blank card (fresh
        # generated avatar, chosen name, identity now on disk). The
        # popup rides the fork's show_qr pattern; the boot's
        # client_identity lifecycle is untouched (read-only from_file).
        Clock.schedule_once(lambda dt: self.show_blank_card(), 0.4)
        # tay (2026-11): the staged hub destination, when typed, is the
        # connect target; an empty hub field keeps the community-hub
        # default so a fresh install is never locked out.
        hub, node_hash, delivery_hash = (hub_data or ("", "", ""))
        if (hub or "").strip():
            self.connect_to_guild(hub, self.username,
                                  node_hash=node_hash or "",
                                  delivery_hash=delivery_hash or "")
        else:
            self._auto_connect_default_hub()

    def _blank_card_payload(self, ident):
        """Payload for the onboarding blank card; None if unusable.

        ident: an RNS.Identity (read-only handle; never assigned).
        """
        try:
            return profile_card.build_payload(
                ident.get_public_key(), self.username)
        except profile_card.CardPayloadError as e:
            print("blank card payload error:", e)
            return None
        except Exception as exc:
            print("blank card payload unavailable:", repr(exc))
            return None

    def show_blank_card(self):
        """Display the onboarding blank card QR (card slice 2).

        Loads the on-disk identity READ-ONLY to build the payload - the
        router boot reloads the same file later, so no lifecycle clash.
        """
        if not os.path.isfile(self._identity_file()):
            return
        ident = RNS.Identity.from_file(self._identity_file())
        if ident is None:
            return
        payload = self._blank_card_payload(ident)
        if payload is None:
            return
        self.show_qr(payload, "Your card",
                     "Anyone who scans this can reach you by mesh - "
                     "the card carries your keyset, not your location.")

    def _identity_file(self):
        return os.path.join(_app_data_dir(), "rns", "identity")

    def _ensure_identity_file(self):
        path = self._identity_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.isfile(path):
            RNS.Identity().to_file(path)
            os.chmod(path, 0o600)
        elif RNS.Identity.from_file(path) is None:
            raise ValueError("identity is damaged; restore a recovery kit")
        return path

    def start_fresh_identity(self):
        """Explicit path when a previous install has no recoverable key."""
        if os.path.exists(self._identity_file()):
            self._set_status("An identity is already installed on this device.")
            return
        self.root.current = "onboarding"
        self.root.get_screen("onboarding").stage = 0

    def _create_generated_avatar(self, name):
        """A deterministic node pattern, unique to the chosen local name."""
        digest = hashlib.sha256(name.encode("utf-8")).digest()
        image = PILImage.new("RGB", (128, 128), (12, 26, 33))
        draw = PILImageDraw.Draw(image)
        nodes = [(24 + digest[i] % 75, 24 + digest[i + 1] % 75)
                 for i in (0, 2, 4)]
        draw.line(nodes + nodes[:1], fill=(76, 205, 179), width=3)
        for i, (x, y) in enumerate(nodes):
            color = (251, 183, 99) if i == 0 else (89, 224, 191)
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=color)
        path = os.path.join(_app_data_dir(), "avatar_generated.jpg")
        image.save(path, "JPEG", quality=82)
        self.my_avatar_small = path
        self.avatar_preview = path
        self._generated_avatar_pending = True

    def _upload_generated_avatar(self):
        if not getattr(self, "_generated_avatar_pending", False):
            return
        path = getattr(self, "my_avatar_small", "")
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "rb") as f:
                result = self.guild_api.avatar_upload(f.read())
            if result and result.get("ok") and not result.get("unconfirmed"):
                self._generated_avatar_pending = False
        except Exception as e:
            print("generated avatar upload pending:", repr(e))

    def _on_live_slash(self, inst, value):
        """tay (2026-11): '/' pops the command palette for live
        selections. Only a bare '/' triggers it: anything longer has
        been completed by a pick or is a literal command line and keeps
        the normal send path. A pick sets the suppression flag before it
        writes the completed command back, so the write-back never
        re-opens the palette."""
        if getattr(self, "_suppress_live_slash", False):
            return
        if getattr(self, "_palette_popup", None) is not None:
            return
        try:
            text = (value or "").strip()
        except Exception:
            return
        if text == "/":
            self.open_command_palette(seed="/")

    def open_command_palette(self, seed=None):
        """Tier-2 command palette (tay's correction 2026-11): the
        slash-command surface for bot & script integration.

        Lists the client-local verbs plus the hub dispatcher's table
        (the /help text, fetched through /api/v1/command like sHades.py
        2733-2751 renders it). A pick prefills msg_input with the
        command so the argument is completed by the user and the send
        path stays exactly the existing slash behavior.
        """
        from kivymd.uix.button import MDButton, MDButtonText
        from kivymd.uix.textfield import MDTextField, MDTextFieldHintText
        self._slash_hub_rows = getattr(self, "_slash_hub_rows", None)

        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))
        note = Label(text="", color=self.theme_colors["text_muted"],
                     font_size=sp(11), size_hint_y=None, height=dp(18))
        grid = BoxLayout(orientation="vertical", size_hint_y=None,
                         spacing=dp(6))
        grid.bind(minimum_height=grid.setter("height"))
        sv = ScrollView()
        sv.add_widget(grid)

        filt = MDTextField(mode="filled", size_hint_y=None, height=dp(56),
                           fill_color_normal=self.theme_colors["input_bg"],
                           fill_color_focus=self.theme_colors["input_bg_focus"],
                           text_color_normal=self.theme_colors["text"],
                           text_color_focus=self.theme_colors["text"],
                           cursor_color=self.theme_colors["text"])
        filt.add_widget(MDTextFieldHintText(text="Type to filter commands"))
        box.add_widget(filt)
        box.add_widget(sv)
        box.add_widget(note)

        popup = Popup(title="Slash commands", content=box,
                      size_hint=(0.9, 0.75))
        self._palette_popup = popup
        popup.bind(on_dismiss=lambda *_: setattr(self, "_palette_popup", None))

        def _prefill(inst=None, cmd=""):
            popup.dismiss()
            try:
                main = self.root.get_screen("main")
                inp = main.ids.msg_input
            except Exception:
                return
            # tay (2026-11): the write-back would re-fire the live-slash
            # watcher (the completed text also starts with '/'); suppress
            # for this frame so the pick finalizes instead of reopening.
            self._suppress_live_slash = True
            inp.text = cmd + " "

            def focus(dt):
                try:
                    inp.focus = True
                except Exception:
                    pass
            Clock.schedule_once(focus, 0)
            Clock.schedule_once(
                lambda _dt: setattr(self, "_suppress_live_slash", False), 0)

        def render(*_):
            query = (filt.text or "").strip().lower()
            grid.clear_widgets()
            for cmd, desc in list(self._slash_client_rows()) + \
                    list(self._slash_hub_rows or []):
                if query and query not in cmd.lower() \
                        and query not in (desc or "").lower():
                    continue
                btn = MDButton(style="tonal")
                btn.size_hint = (1, None)
                btn.height = dp(40)
                btn.bind(on_press=lambda inst, c=cmd: _prefill(inst, c))
                btn.add_widget(MDButtonText(
                    text=("%s %s" % (cmd, desc)).strip() if desc else cmd,
                    font_size=sp(12),
                    text_color=self.theme_colors["text"]))
                grid.add_widget(btn)

        def fetch_hub(*_):
            if self._slash_hub_rows is not None or not self.guild_api:
                return

            def work():
                try:
                    res = self.guild_api.command("help")
                    out = (res or {}).get("output") if res.get("ok") else ""
                    Clock.schedule_once(lambda dt: self._accept_hub_rows(
                        parse_slash_help(out or ""), render), 0)
                except Exception:
                    Clock.schedule_once(
                        lambda dt: self._accept_hub_rows([], render), 0)
            threading.Thread(target=work, daemon=True).start()

        filt.bind(text=render)
        render(filt, None)
        import ui_fx
        ui_fx.popup_entrance(popup)
        # tay (2026-11): a live '/'-trigger seeds the filter from the
        # channel input and takes the keyboard, so the palette is the
        # selection surface while typing.
        if seed:
            filt.text = seed
        filt.focus = True
        note.text = "client commands + hub dispatcher (fetching...)"
        Clock.schedule_once(lambda dt: fetch_hub(), 0)

    def open_recovery_sheet(self, onboarding_name=None):
        """Make an encrypted kit before first connection or from Profile."""
        box = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))
        hint = Label(text="Save this kit somewhere outside your phone. The phrase "
                          "is never stored; losing both means losing your identity.",
                     color=self.theme_colors["text_muted"], font_size=sp(13),
                     size_hint_y=None, height=dp(64), halign="center",
                     valign="middle")
        hint.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        box.add_widget(hint)

        def field(label):
            widget = TextInput(password=True, multiline=False, hint_text=label,
                               font_name="MeshBody", font_size=sp(16),
                               foreground_color=self.theme_colors["text"],
                               hint_text_color=self.theme_colors["text_muted"],
                               background_normal="", background_active="",
                               background_color=self.theme_colors["input_bg"],
                               size_hint_y=None, height=dp(46))
            box.add_widget(widget)
            return widget

        phrase = field("Recovery phrase (12+ characters)")
        confirm = field("Repeat recovery phrase")
        status = Label(text="", color=self.theme_colors["accent"],
                       font_size=sp(12), size_hint_y=None, height=dp(44))
        status.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        box.add_widget(status)
        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        save = Button(text="Save kit", background_normal="",
                      background_color=self.theme_colors["primary"],
                      color=self.theme_colors["btn_text"])
        later = Button(text="Skip for now" if onboarding_name is not None else "Close",
                       background_normal="", background_color=self.theme_colors["sidebar_ctrl"],
                       color=self.theme_colors["text"])
        actions.add_widget(save)
        actions.add_widget(later)
        box.add_widget(actions)
        sheet = Popup(title="Protect your identity", content=box,
                      size_hint=(0.86, 0.60), auto_dismiss=False)

        def done(success, detail):
            status.text = detail
            save.disabled = False
            if success and onboarding_name is not None:
                sheet.dismiss()
                self.complete_onboarding(
                    onboarding_name, getattr(self, "_onboard_hub", None))
                self._set_status(detail)

        def save_kit(*_):
            if phrase.text != confirm.text:
                status.text = "The phrases do not match."
                return
            try:
                identity_path = self._ensure_identity_file()
                with open(identity_path, "rb") as stream:
                    key_bytes = stream.read(4097)
                profile = {"username": (onboarding_name or self.username or "")[:32],
                           "friends": self._friends[:200],
                           "servers": self._servers[:50]}
                kit = recovery.create_kit(key_bytes, phrase.text, profile)
            except Exception as exc:
                status.text = "Kit could not be made: %s" % exc
                return
            save.disabled = True
            self._export_recovery_bytes(kit, done)

        save.bind(on_press=save_kit)
        later.bind(on_press=lambda *_: (sheet.dismiss(),
                   self.complete_onboarding(
                       onboarding_name, getattr(self, "_onboard_hub", None)))
                   if onboarding_name is not None
                   else sheet.dismiss())
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def show_qr(self, payload, title, note=""):
        """Display a scannable QR without exposing the payload as plain text."""
        fd, path = tempfile.mkstemp(prefix="shades-qr-", suffix=".png",
                                    dir=_app_data_dir())
        os.close(fd)
        try:
            qr_tools.make_qr(payload, path)
        except Exception:
            os.unlink(path)
            raise
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))
        box.add_widget(Image(source=path, allow_stretch=True))
        if note:
            box.add_widget(Label(text=note, color=self.theme_colors["text_muted"],
                                 font_size=sp(12), size_hint_y=None, height=dp(46)))
        popup = Popup(title=title, content=box, size_hint=(0.9, 0.72))
        popup.bind(on_dismiss=lambda *_: os.path.exists(path) and os.unlink(path))
        import ui_fx
        ui_fx.popup_entrance(popup)

    def show_invite_qr(self):
        host, port = getattr(self, "hub_host", None), getattr(self, "hub_port", None)
        node = getattr(self, "guild_node_hash", None)
        if not (host and port and node):
            self._set_status("Connect to a guild to display its invite QR.")
            return
        self.show_qr(invite.build_invite(host, port, node), "Guild invite",
                     "Scan with sHades or a device camera.")

    def open_transfer_qr(self):
        """One QR carries the encrypted key and local name, no chat or friends."""
        box = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(14))
        box.add_widget(Label(text="Choose a 12+ character phrase. The QR contains "
                             "your encrypted identity. Only show it to your second device.",
                             color=self.theme_colors["text"], font_size=sp(14),
                             text_size=(dp(280), None)))
        phrase = TextInput(password=True, multiline=False, hint_text="Transfer phrase",
                           size_hint_y=None, height=dp(44))
        box.add_widget(phrase)
        show = Button(text="Show transfer QR", size_hint_y=None, height=dp(46))
        box.add_widget(show)
        sheet = Popup(title="Transfer identity", content=box, size_hint=(0.86, 0.4))

        def create(*_):
            try:
                with open(self._ensure_identity_file(), "rb") as source:
                    identity = source.read(4097)
                kit = recovery.create_kit(
                    identity, phrase.text,
                    {"username": self.username or "",
                     "friends": [], "servers": []})
                payload = qr_tools.encode_kit(kit)
                sheet.dismiss()
                self.show_qr(payload, "Encrypted identity",
                             "Scan on your second device, then enter the phrase there.")
            except Exception as exc:
                self._set_status("Transfer QR failed: %s" % exc)
        show.bind(on_press=create)
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def scan_qr(self, on_value):
        """Open a camera QR scanner where its native decoder is installed."""
        if self._android_platform():
            try:
                from android.permissions import request_permissions, Permission

                def ready(_permissions, grants):
                    if all(grants):
                        Clock.schedule_once(lambda _dt: self._open_qr_scanner(on_value), 0)
                    else:
                        Clock.schedule_once(lambda _dt: self._set_status(
                            "Camera permission is needed to scan a QR."), 0)
                request_permissions([Permission.CAMERA], ready)
            except Exception as exc:
                self._set_status("Camera permission request failed: %s" % exc)
            return
        self._open_qr_scanner(on_value)

    def _open_qr_scanner(self, on_value):
        try:
            from kivy_garden.zbarcam import ZBarCam
            scanner = ZBarCam()
        except Exception as exc:
            self._set_status("Camera scanner unavailable: %s" % exc)
            return
        box = BoxLayout(orientation="vertical", padding=dp(8))
        box.add_widget(scanner)
        sheet = Popup(title="Scan a sHades QR", content=box, size_hint=(0.95, 0.8))
        seen = {"done": False}

        def decoded(_scanner, symbols):
            if seen["done"] or not symbols:
                return
            try:
                value = symbols[0].data.decode("utf-8")
            except (UnicodeError, AttributeError):
                self._set_status("QR is not text")
                return
            seen["done"] = True
            sheet.dismiss()
            on_value(value)
        scanner.bind(symbols=decoded)
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def scan_invite_qr(self):
        def apply(value):
            if not invite.parse_invite(value):
                self._set_status("This QR is not a guild invite.")
                return
            self.root.current = "connect"
            self.root.get_screen("connect").ids.hub_address.text = value
            self._set_status("Invite scanned. Choose your name and connect.")
        self.scan_qr(apply)

    def _export_recovery_bytes(self, kit, done, filename="sHades-recovery.shades",
                               description="Encrypted kit"):
        """Use the system picker or Downloads; kit may be bytes or a file path."""
        if self._android_platform():
            try:
                from jnius import autoclass
                from android import activity as android_activity
                if not getattr(self, "_recovery_export_bound", False):
                    android_activity.bind(on_activity_result=self._recovery_export_result)
                    self._recovery_export_bound = True
                self._recovery_export_pending = (kit, done, description)
                Intent = autoclass("android.content.Intent")
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                intent = Intent(Intent.ACTION_CREATE_DOCUMENT)
                intent.addCategory(Intent.CATEGORY_OPENABLE)
                intent.setType("application/octet-stream")
                intent.putExtra(Intent.EXTRA_TITLE, filename)
                activity.startActivityForResult(intent, 0x5A1A)
            except Exception as exc:
                self._recovery_export_pending = None
                done(False, "Document picker failed: %s" % exc)
            return
        folder = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        stem, extension = os.path.splitext(filename)
        filename = "%s-%s%s" % (stem, time.strftime("%Y%m%d-%H%M%S"), extension)
        path = os.path.join(folder, filename)
        try:
            if isinstance(kit, str):
                if os.path.exists(path):
                    raise FileExistsError(path)
                shutil.copyfile(kit, path)
                os.chmod(path, 0o600)
            else:
                recovery.write_private(path, kit)
            done(True, "%s saved to %s" % (description, path))
        except Exception as exc:
            done(False, "Save failed: %s" % exc)

    def _recovery_export_result(self, request_code, result_code, intent):
        if request_code != 0x5A1A:
            return
        pending = getattr(self, "_recovery_export_pending", None)
        self._recovery_export_pending = None
        if pending is None:
            return

        def finish(_dt):
            kit, done, description = pending
            if result_code != -1 or intent is None:
                done(False, "Kit save canceled.")
                return
            try:
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                uri = intent.getData()
                pfd = activity.getContentResolver().openFileDescriptor(uri, "w")
                fd = pfd.detachFd()
                with os.fdopen(fd, "wb") as stream:
                    if isinstance(kit, str):
                        with open(kit, "rb") as source:
                            shutil.copyfileobj(source, stream)
                    else:
                        stream.write(kit)
                    stream.flush()
                pfd.close()
                done(True, "%s saved." % description)
            except Exception as exc:
                done(False, "Kit write failed: %s" % exc)
        Clock.schedule_once(finish, 0)

    def open_restore_sheet(self):
        """Choose an encrypted kit and restore only onto a device with no key."""
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))
        phrase = TextInput(password=True, multiline=False,
                           hint_text="Recovery phrase", font_name="MeshBody",
                           foreground_color=self.theme_colors["text"],
                           hint_text_color=self.theme_colors["text_muted"],
                           background_normal="", background_active="",
                           background_color=self.theme_colors["input_bg"],
                           size_hint_y=None, height=dp(46))
        box.add_widget(phrase)
        status = Label(text="Choose the encrypted .shades kit.",
                       color=self.theme_colors["text_muted"],
                       size_hint_y=None, height=dp(44), font_size=sp(12))
        box.add_widget(status)
        chooser = None
        if not self._android_platform():
            chooser = FileChooserListView(path=os.path.join(
                os.path.expanduser("~"), "Downloads"))
            if not os.path.isdir(chooser.path):
                chooser.path = os.path.expanduser("~")
            box.add_widget(chooser)
        actions = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        restore_btn = Button(text="Choose kit" if self._android_platform() else "Restore selected",
                             background_normal="", background_color=self.theme_colors["primary"],
                             color=self.theme_colors["btn_text"])
        cancel = Button(text="Cancel", background_normal="",
                        background_color=self.theme_colors["sidebar_ctrl"],
                        color=self.theme_colors["text"])
        actions.add_widget(restore_btn)
        actions.add_widget(cancel)
        box.add_widget(actions)
        sheet = Popup(title="Restore identity", content=box,
                      size_hint=(0.9, 0.75), auto_dismiss=False)

        def restore(path):
            try:
                complete = self._restore_recovery_file(path, phrase.text)
            except Exception as exc:
                status.text = "Restore failed: %s" % exc
                return
            sheet.dismiss()
            self.root.current = "connect"
            if complete and self._enter_restored_account():
                # the account transfers alive; the connect attempt set
                # its own status
                pass
            elif complete:
                self._prefill_connect_from_servers()
                self._set_status("Identity recovered. Connect to a saved guild.")
            else:
                self._prefill_connect_from_servers()
                self._set_status(
                    "Identity recovered, but some profile details could not be saved.")

        def pick(*_):
            if self._android_platform():
                self._saf_pick(0x5A19, restore,
                               lambda reason: setattr(status, "text", reason))
            elif chooser and chooser.selection:
                restore(chooser.selection[0])
            else:
                status.text = "Select a kit file first."

        scan_btn = Button(text="Scan transfer QR",
                          size_hint_y=None, height=dp(44))

        def accept_qr(value):
            try:
                payload = qr_tools.decode_kit(value)
                fd, temp = tempfile.mkstemp(
                    prefix="shades-scan-", suffix=".shades",
                    dir=_app_data_dir())
                with os.fdopen(fd, "wb") as target:
                    target.write(payload)
                try:
                    restore(temp)
                finally:
                    os.unlink(temp)
            except Exception as exc:
                status.text = "QR restore failed: %s" % exc
        scan_btn.bind(on_press=lambda *_: self.scan_qr(accept_qr))
        box.add_widget(scan_btn)

        restore_btn.bind(on_press=pick)
        cancel.bind(on_press=lambda *_: sheet.dismiss())
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def _restore_recovery_file(self, path, passphrase):
        if os.path.exists(self._identity_file()):
            raise FileExistsError("an identity is already installed")
        if os.path.getsize(path) > recovery.MAX_KIT_BYTES:
            raise ValueError("kit is too large")
        with open(path, "rb") as stream:
            identity, profile = recovery.open_kit(stream.read(), passphrase)
        # Verify RNS understands the key before installing it permanently.
        folder = os.path.dirname(self._identity_file())
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".verify-", dir=folder)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(identity)
            if RNS.Identity.from_file(tmp) is None:
                raise ValueError("kit contains an invalid identity")
        finally:
            os.unlink(tmp)
        recovery.write_private(self._identity_file(), identity)
        metadata_ok = True
        name = profile.get("username")
        if isinstance(name, str):
            self.username = re.sub(r"[^a-zA-Z0-9_. -]", "", name).strip()[:24]
        friends = profile.get("friends")
        if isinstance(friends, list):
            self._friends = [f for f in friends[:200] if isinstance(f, dict)
                             and dms.is_user_hash(f.get("user_hash"))]
            try:
                dms.save_friends(self._friends_path, self._friends)
            except OSError:
                metadata_ok = False
        servers = profile.get("servers")
        if isinstance(servers, list):
            self._servers = [s for s in servers[:50] if isinstance(s, dict)
                             and isinstance(s.get("hub_address"), str)
                             and 0 < len(s["hub_address"]) <= 256]
            metadata_ok = self._save_servers() and metadata_ok
            self._active_server_idx = 0 if self._servers else -1
        # tay's transfer contract (0.36.24, re-applied at 0.36.25): the restored device resumes
        # exactly like the device the kit came from. The kit's
        # last-active guild becomes the saved session, so the next boot
        # reconnects instead of re-staging, and the portable profile
        # exists so the boot gate routes the restored key as a
        # completed profile rather than fresh staging.
        self._portable_name = self.username
        try:
            self._save_portable_profile()
        except OSError:
            metadata_ok = False
        first = self._servers[0] if self._servers else None
        port = (first or {}).get("port")
        if first and isinstance(port, int) and port > 0 and first.get(
                "hub_address"):
            self.hub_address = first["hub_address"]
            self.hub_host = first.get("host") or ""
            self.hub_port = port
            self.guild_node_hash = first.get("node_hash") or None
            self.delivery_hash = first.get("delivery_hash") or None
            self.username = self.username or first.get("username") or ""
            self._save_session()
        try:
            recovery.write_private(self._onboarding_path(),
                                   json.dumps({"name": self.username,
                                               "completed": True}).encode(), replace=True)
        except OSError:
            metadata_ok = False
        return metadata_ok

    def _prefill_connect_from_servers(self):
        """Fill the connect screen from the kit's first server row, so a
        restore the flow cannot auto-dial still opens with the restored
        destination ready to edit and tap."""
        if not self._servers:
            return
        try:
            scr = self.root.get_screen("connect")
            first = self._servers[0]
            scr.ids.hub_address.text = first.get("hub_address") or ""
            scr.ids.username.text = self.username or first.get("username") or ""
            scr.ids.node_hash.text = first.get("node_hash") or ""
            scr.ids.delivery_hash.text = first.get("delivery_hash") or ""
        except Exception:
            pass

    def _enter_restored_account(self):
        """tay's transfer contract (0.36.24, re-applied at 0.36.25): a restored kit whose server
        row is complete transfers the account ALIVE - dial the kit's
        last-active guild exactly as the device it came from. Returns
        True when an attempt started.

        P11h (device receipt, 0.36.24: the import parked on connect with
        the hub field empty and 'Identity recovered. Connect to a saved
        guild.'): a restored kit with NO dialable server row returned
        False here and left the member on the connect screen - hub
        blank, Connect disabled - while the restart path auto-dialed
        the default hub through the portable-profile fallthrough
        (_connect_default_hub's 'user' fallback). Post-import now
        behaves exactly like post-restart: dial the default hub through
        connect_to_guild (its Clock law keeps RNS.Reticulum on the main
        thread even when the SAF result callback is not); the restored
        identity rides _init_identity_and_lxmf unchanged."""
        first = self._servers[0] if self._servers else None
        if not (first and first.get("hub_address")):
            username = (self.username or "").strip() or "user"
            self.connect_to_guild(DEFAULT_HUB_ADDRESS, username)
            return True
        port = first.get("port")
        if not (isinstance(port, int) and port > 0):
            return False
        try:
            self.root.current = "connect"
        except Exception:
            pass
        self._prefill_connect_from_servers()
        username = self.username or first.get("username") or ""
        self.connect_to_guild(first.get("hub_address"), username,
                              first.get("node_hash") or "",
                              first.get("delivery_hash") or "")
        return True

    def _auto_connect_default_hub(self):
        """Connect to the default community hub on a fresh install.

        Leaves the user on the connect screen while the attempt runs. If the
        hub cannot be reached, the status line explains why and the user can
        enter a different server -- the default is a convenience, never a
        hard lock-in.
        """
        self._set_status("Connecting to default hub %s..." % DEFAULT_HUB_ADDRESS)
        try:
            self.root.current = "connect"
        except Exception:
            pass
        # Give the connect screen a moment to render its status line before
        # the (blocking) RNS work starts on a worker thread.
        Clock.schedule_once(lambda dt: self._connect_default_hub(), 0.2)

    def _connect_default_hub(self):
        """Start the connect flow against the default hub.

        Runs on the MAIN thread (it is invoked from a Clock callback):
        _init_reticulum() constructs RNS.Reticulum(), which installs signal
        handlers and is only legal on the main thread. The blocking link
        attempt is moved to a worker by _init_reticulum() itself.
        """
        try:
            username = (self.username or "").strip() or "user"
            self.hub_address = DEFAULT_HUB_ADDRESS
            self.hub_host = DEFAULT_HUB_HOST
            self.hub_port = DEFAULT_HUB_PORT
            self.username = username
            self.guild_node_hash = DEFAULT_GUILD_NODE_HASH or None
            self.node_hash = self.guild_node_hash
            self.delivery_hash = None
            # Add the default hub to the server list so it shows up in the
            # server sidebar and can be switched to later.
            self._add_server(DEFAULT_HUB_ADDRESS, username,
                             DEFAULT_GUILD_NODE_HASH or "")
            self._save_session()
            self._init_reticulum()
        except Exception as e:
            print("default hub connect error:", repr(e))
            err = "Could not reach default hub %s: %s" % (DEFAULT_HUB_ADDRESS, e)
            Clock.schedule_once(lambda dt: self._set_status(err), 0)
            Clock.schedule_once(lambda dt: self._return_to_connect_screen(err), 0)

    def _return_to_connect_screen(self, message=""):
        """Fall back to the connect screen after a failed auto-connect."""
        try:
            self.root.current = "connect"
        except Exception:
            pass
        if message:
            self._set_status(message)

    def _open_main(self):
        """Switch to the main chat screen."""
        try:
            self.root.current = "main"
            self._restore_avatar_from_disk()
            self._refresh_channels_ui()
            self._refresh_server_sidebar()
            # H4: render the offline cache at boot even with no guild_api
            # (dead hub). The old path only rendered history once the
            # guild API was up (_load_history_ui rescheduled forever
            # while guild_api was None), so a dead hub left an empty
            # channel although cached messages existed.
            try:
                main = self.root.get_screen("main")
                grid = main.ids.message_grid
                name = getattr(main, "_current_channel", "general")
                if self._show_cached_channel(grid, self._channel_cid(name)):
                    Clock.schedule_once(
                        lambda dt: self._scroll_to_bottom(), 0.1)
            except Exception as e:
                print("open main cache render error:", repr(e))
        except Exception as e:
            print("open main error:", repr(e))

    def show_fullscreen_image(self, source, embed=None):
        """Show an image in full-screen overlay with dark background and download/close buttons.

        Tracks the overlay so it can be dismissed (minimized) later, and
        records which embed is expanded so it can be reset on minimize.
        """
        # Dismiss any existing overlay first
        if getattr(self, "_fullscreen_overlay", None) is not None:
            self.dismiss_fullscreen_image(None)
        overlay = FullScreenImageOverlay(source=source, embed=embed)
        self._fullscreen_overlay = overlay
        # Parent to Window so the overlay covers the entire display regardless
        # of which screen is current. Parenting to a Screen can leave the
        # overlay undersized (Screen may not propagate size to late-added
        # children), which made its buttons appear floating in the corner.
        Window.add_widget(overlay)

    def dismiss_fullscreen_image(self, embed):
        """Remove the full-screen overlay and reset the embed's expanded flag."""
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            try:
                if overlay.parent:
                    overlay.parent.remove_widget(overlay)
            except Exception:
                pass
            self._fullscreen_overlay = None
        if embed is not None:
            embed.is_expanded = False

    # -- session ---------------------------------------------------------
    def _session_path(self):
        d = _app_data_dir()
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "session.json")

    def _save_session(self):
        # Hardening only (fixes.md #17, plaintext half): the write is
        # already atomic (tmp + os.replace); the session file now also
        # gets owner-only permissions so other local users cannot read
        # the stored hub address/hashes. This is NOT encryption -- a
        # device-key scheme needs Jeffery's call, so none is designed
        # here. A failed write removes the stray tmp file and leaves
        # the previous session intact.
        tmp = None
        try:
            path = self._session_path()
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    # The GUILD's node hash, not our own. self.node_hash is
                    # reused later for this client's own nomadnetwork.node
                    # destination (see _connect_after_rns), so saving it here
                    # would persist OUR hash and the next resume would look
                    # up the wrong identity. guild_node_hash is the value
                    # that actually identifies the guild.
                    "node_hash": self.guild_node_hash,
                    "delivery_hash": self.delivery_hash,
                    "username": self.username,
                    "hub_address": self.hub_address,
                    "hub_host": self.hub_host,
                    "hub_port": self.hub_port,
                }, f)
            os.replace(tmp, path)  # atomic on POSIX and Windows
            tmp = None  # replaced: nothing to clean up on failure below
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _load_session(self):
        try:
            with open(self._session_path()) as f:
                return json.load(f)
        except Exception:
            return None

    # -- server/guild list persistence ---------------------------------------
    def _load_servers(self):
        try:
            with open(self._servers_path) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_servers(self):
        # fork merge: explicit success return (adopted from the fork's
        # portability contract); existing fire-and-forget callers ignore it.
        tmp = None
        try:
            os.makedirs(_app_data_dir(), exist_ok=True)
            tmp = self._servers_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._servers, f, indent=2)
            os.replace(tmp, self._servers_path)
            return True
        except Exception as e:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            print("save servers failed:", repr(e))
            return False

    def _add_server(self, hub_address, username, node_hash="",
                    delivery_hash="", display_name=None, avatar_path=None):
        """Add or update a server in the list."""
        inv = invite.parse_invite(hub_address)
        if inv:
            host = inv.get("host") or hub_address
            port = inv.get("port")
        else:
            # Plain ip:port format (no shades:// prefix)
            if ":" in hub_address:
                host_part, _, port_part = hub_address.rpartition(":")
                host = host_part.strip()
                try:
                    port = int(port_part.strip()) if port_part.strip().isdigit() else None
                except ValueError:
                    port = None
            else:
                host = hub_address
                port = None
        # Remember whether the caller named this server explicitly. When it
        # did not, the auto-derived host fallback must not overwrite a
        # display name the user already set (connect_to_guild() re-adds the
        # server without one).
        explicit_name = bool(display_name)
        # Generate a display name from host if not provided
        if not display_name:
            display_name = host.split(":")[0] if ":" in host else host
        entry = {
            "hub_address": hub_address,
            "host": host,
            "port": port,
            "node_hash": node_hash,
            "delivery_hash": delivery_hash,
            "username": username,
            "display_name": display_name,
            "avatar_path": avatar_path,
        }
        # Check if server already exists (by host+port)
        for i, s in enumerate(self._servers):
            if s.get("host") == host and s.get("port") == port:
                if not explicit_name and s.get("display_name"):
                    entry["display_name"] = s["display_name"]
                if avatar_path is None and s.get("avatar_path"):
                    entry["avatar_path"] = s["avatar_path"]
                # rail icon state (task 6): an update must not wipe the icon
                for key in ("icon_path", "icon_blob"):
                    if s.get(key):
                        entry[key] = s[key]
                # tay's device report (0.36.24): a reconnect that types
                # only ip:port must not wipe the guild hashes a restored
                # kit carried in this row; the connect flow re-adopts
                # them from the returned row.
                for key in ("node_hash", "delivery_hash"):
                    if not entry.get(key) and s.get(key):
                        entry[key] = s[key]
                self._servers[i] = entry
                self._active_server_idx = i
                self._save_servers()
                return i
        self._servers.append(entry)
        self._active_server_idx = len(self._servers) - 1
        self._save_servers()
        return self._active_server_idx

    def _remove_server(self, idx):
        if 0 <= idx < len(self._servers):
            self._servers.pop(idx)
            if self._active_server_idx >= len(self._servers):
                self._active_server_idx = len(self._servers) - 1
            self._save_servers()

    def _set_active_server(self, idx):
        if 0 <= idx < len(self._servers):
            self._active_server_idx = idx
            s = self._servers[idx]
            self.hub_address = s.get("hub_address")
            self.hub_host = s.get("host")
            self.hub_port = s.get("port")
            self.guild_node_hash = s.get("node_hash")
            self.node_hash = self.guild_node_hash
            self.delivery_hash = s.get("delivery_hash")
            saved_username = s.get("username", "") or ""
            self.username = ("" if saved_username == "user" else saved_username)
            self._save_session()

    # -- message history persistence (so embeds survive close/re-open) -----
    def _history_path(self):
        return os.path.join(_app_data_dir(), "history.json")

    def _offline_path(self):
        return os.path.join(_app_data_dir(), "offline.sqlite3")

    def _cache_guild(self):
        node = getattr(self, "guild_node_hash", None)
        address = getattr(self, "hub_address", None)
        return ("node:" + str(node)) if node else (
            "hub:" + str(address) if address else "")

    def _post_was_purged(self, guild, message):
        author, cutoff = self._purged_before.get(guild, (None, 0))
        return (message.get("deleted_at") or
                (author and message.get("author_hash") == author and
                 float(message.get("ts") or 0) <= cutoff))

    def _queue_cache(self, message):
        guild = self._cache_guild()
        if not guild or guild in self._forgotten_cache_guilds:
            return
        if self._post_was_purged(guild, message):
            return
        try:
            channel = (message.get("channel_id") or
                       self._channel_cid(getattr(
                           self.root.get_screen("main"), "_current_channel",
                           "general")))
            msg_id = message.get("msg_id")
            if not guild or not channel or not msg_id or \
                    str(msg_id).startswith("pending:"):
                return
            self._cache_pending[(guild, channel, msg_id)] = dict(message)
            if not getattr(self, "_cache_event", None):
                self._cache_event = Clock.schedule_once(self._flush_cache, 0.4)
        except Exception:
            pass

    def _flush_cache(self, _dt):
        pending = getattr(self, "_cache_pending", {})
        self._cache_pending, self._cache_event = {}, None
        batches = {}
        for (guild, channel, _msg_id), message in pending.items():
            batches.setdefault((guild, channel), []).append(message)

        def work():
            for (guild, channel), messages in batches.items():
                try:
                    with self._cache_write_lock:
                        if guild not in self._forgotten_cache_guilds:
                            offline_store.put(
                                self._offline_path(), guild, channel,
                                [m for m in messages
                                 if not self._post_was_purged(guild, m)])
                except Exception as exc:
                    print("offline cache write failed:", repr(exc))
        if batches:
            threading.Thread(target=work, daemon=True).start()

    def _show_cached_channel(self, grid, cid):
        """Fork merge: render the offline cache before the network answers.
        _render_message dedupes by msg_id and the fetch path refreshes edited
        rows from the server copy, so the server still wins on conflicts."""
        try:
            messages = offline_store.recent(self._offline_path(),
                                            self._cache_guild(), cid)
        except Exception as exc:
            print("offline cache read failed:", repr(exc))
            return False
        rendered = 0
        for m in messages:
            try:
                self._render_message(grid, m)
                rendered += 1
            except Exception:
                pass
        if rendered:
            self._regroup_messages(grid)
        return rendered > 0

    def _add_to_history(self, m):
        if not hasattr(self, "_history"):
            self._history = []
        self._history = [x for x in self._history if x.get("msg_id") != m.get("msg_id")]
        if m.get("msg_id"):
            self._history.append(m)
        if len(self._history) > 500:
            self._history = self._history[-500:]

    def _save_history(self):
        try:
            hist = []
            for m in getattr(self, "_history", []):
                entry = dict(m)
                kept = []
                for att in entry.get("attachments") or []:
                    if isinstance(att, dict) and att.get("kind") == ATT_EPHEMERAL:
                        # blob already on disk; keep the local_path reference
                        lp = att.get("local_path")
                        if lp and os.path.exists(lp):
                            kept.append(att)
                    else:
                        kept.append(att)
                entry["attachments"] = kept
                hist.append(entry)
            with open(self._history_path(), "w") as f:
                json.dump(hist, f)
        except Exception as e:
            print("save history error:", e)

    def _load_history(self):
        try:
            with open(self._history_path()) as f:
                return json.load(f) or []
        except Exception:
            return []

    def _load_history_ui(self, dt=0):
        """Re-render persisted messages once the guild API is available."""
        if not getattr(self, "guild_api", None):
            Clock.schedule_once(self._load_history_ui, 1.0)
            return
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
        except Exception:
            grid = None
        if grid is None:
            Clock.schedule_once(self._load_history_ui, 0.5)
            return
        for m in self._load_history():
            self._render_message(grid, m)
        Clock.schedule_once(lambda dt: self._scroll_to_bottom(), 0.1)
        if not self._scroll_bound:
            self._scroll_bound = True
            try:
                scroll = main.ids.scroll
                scroll.bind(scroll_y=lambda inst, val: self._on_scroll_y(val))
            except Exception:
                pass

    # -- connect ----------------------------------------------------------
    def _invite_prefill(self, hub_widget, node_widget):
        """U6a: when the hub-address field loses focus holding a shades://
        invite, split it into the address and node-hash fields so the user
        sees what will be dialed. Explicitly-filled hash fields win."""
        if getattr(hub_widget, "focus", False):
            return  # only on focus loss
        inv = invite.parse_invite(hub_widget.text or "")
        if not inv:
            return
        host, port = inv["host"], inv["port"]
        if host is not None:
            hub_widget.text = host if port is None else "%s:%s" % (host, port)
        if inv["node_hash"] and not (node_widget.text or "").strip():
            node_widget.text = inv["node_hash"]

    def connect_to_guild(self, hub_address, username, node_hash="", delivery_hash=""):
        if not (hub_address or "").strip():
            _fail_validation(self, "Hub address required.")
            return
        # tay (2026-11): a username is required before connecting to a
        # hub for the first time; no silent "user" fallback.
        username = (username or "").strip()
        if not username:
            _fail_validation(self, "A username is required before connecting.")
            return
        node_hash = (node_hash or "").strip().lower()
        delivery_hash = (delivery_hash or "").strip().lower()

        # U6a: a shades:// invite pasted into the address field carries the
        # hub endpoint and optionally the guild node hash. Split it here too,
        # so Connect works even without the focus-loss prefill pass.
        inv = invite.parse_invite(hub_address)
        if inv:
            if inv["host"] is not None:
                hub_address = inv["host"] if inv["port"] is None \
                    else "%s:%s" % (inv["host"], inv["port"])
            if inv["node_hash"] and not node_hash:
                node_hash = inv["node_hash"]

        if not hub_address:
            _fail_validation(self, "Hub address required (ip:port)")
            return

        # Parse ip:port
        if ":" not in hub_address:
            _fail_validation(self, "Hub address must be ip:port (e.g. 192.168.1.50:29221)")
            return
        host, port_str = hub_address.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            _fail_validation(self, "Invalid port number")
            return

        if (not host.strip() or any(ch.isspace() for ch in host)
                or not 1 <= port <= 65535):
            _fail_validation(self, "Hub address needs a host and port from 1 to 65535")
            return

        # Validate hashes if provided
        if node_hash and not re.fullmatch(r"[0-9a-f]{32}", node_hash):
            _fail_validation(self, "Invalid node_hash (need 32 hex)")
            return
        if delivery_hash and not re.fullmatch(r"[0-9a-f]{32}", delivery_hash):
            _fail_validation(self, "Invalid delivery_hash (need 32 hex)")
            return

        # Add/update server in the list
        srv_idx = self._add_server(hub_address, username, node_hash, delivery_hash)
        row = self._servers[srv_idx] if 0 <= srv_idx < len(self._servers) else {}
        # tay's device report (0.36.24): a restored kit carries the
        # guild's node/delivery hashes in its server row; a reconnect
        # that only types ip:port inherits them instead of wiping them.
        if not node_hash and row.get("node_hash"):
            node_hash = row["node_hash"]
        if not delivery_hash and row.get("delivery_hash"):
            delivery_hash = row["delivery_hash"]

        self.hub_address = hub_address
        self.hub_host = host
        self.hub_port = port
        # An empty username means "no name chosen yet" -- the unique
        # default is assigned once the client identity exists. A literal
        # "user" would be taken by the first client to save it.
        self.username = ("" if username == "user" else (username or ""))
        self.guild_node_hash = node_hash or None
        self.node_hash = self.guild_node_hash
        self.delivery_hash = delivery_hash or None
        self._set_status("Connecting...")
        self._save_session()
        # H2: one connect at a time. A second connect while one is in
        # flight is ignored -- two concurrent _connect_after_rns workers
        # would race on self.link / self.guild_api.
        if not self._begin_connect():
            _fail_validation(self, "Already connecting...")
            return
        # RNS.Reticulum() installs signal handlers and must run on the main
        # thread. Schedule it there, then do blocking link work in a bg thread.
        # If RNS is already initialized (retry), skip initialization.
        if self.reticulum is not None:
            self._start_connect_after_rns()
        else:
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0)

    def _begin_connect(self):
        """Claim the single in-flight connect slot.

        Returns False when a connect is already running (the caller must
        ignore the new request); True when the slot was claimed. The
        slot is released by the _connect_after_rns worker's finally, or
        by _init_reticulum if RNS construction fails before the worker
        starts -- otherwise a failed init would wedge every later
        connect.
        """
        if getattr(self, "_connect_in_flight", False):
            return False
        self._connect_in_flight = True
        return True

    def _start_connect_after_rns(self):
        """Run _connect_after_rns on a daemon worker, never the main thread.

        H2: _connect_after_rns blocks up to ~150s (60s discover + 30s
        recall wait + 3x20s link attempts). Called from a Clock callback
        it froze the UI for a minute on every connect, server switch and
        re-establish. All UI touches inside it already hop through
        Clock.schedule_once, so the worker is safe; a second worker is
        never spawned while one is alive.
        Returns True when a worker was started.
        """
        worker = getattr(self, "_connect_worker", None)
        if worker is not None and worker.is_alive():
            return False
        self._connect_workers_started = getattr(
            self, "_connect_workers_started", 0) + 1

        def _run():
            try:
                self._connect_after_rns()
            finally:
                self._connect_in_flight = False
        thread = threading.Thread(target=_run, daemon=True)
        self._connect_worker = thread
        thread.start()
        return True

    # -- server sidebar ------------------------------------------------------
    def _refresh_server_sidebar(self):
        """Populate the server grid with rail discs (task 6)."""
        # P5: the active server banner feeds the sidebar strip; the
        # letter tile covers the gap when no banner is set
        active = (self._servers[self._active_server_idx]
                  if 0 <= self._active_server_idx < len(self._servers)
                  else None)
        if active is not None:
            bpath = active.get("banner_path") or ""
            self.banner_channel_path = (bpath
                                        if os.path.isfile(bpath) else "")
            self.banner_letter = (
                (active.get("display_name", active.get("host", ""))
                 or "S")[:1].upper() or "S")
        try:
            main = self.root.get_screen("main")
            grid = main.ids.server_grid
        except Exception:
            return
        grid.clear_widgets()
        for idx, s in enumerate(self._servers):
            display_name = s.get("display_name", s.get("host", "Server"))
            btn = GuildRailButton(display_name=display_name,
                                  icon_path=s.get("icon_path") or "",
                                  active=(idx == self._active_server_idx),
                                  index=idx)
            btn.bind(on_press=lambda inst, i=idx: self._select_server(i))
            grid.add_widget(btn)

    def _ingest_guild_icon(self, blob_id, srv):
        """whoami's guild icon (a blob id) -> blob store -> icons/<host>_<blob
        head>.<ext> -> servers entry -> rail refresh. No-op when the server's
        icon is already that blob; disk writes happen on the caller's worker
        thread, only the widget refresh hops to the main thread."""
        if not blob_id or not srv or not self.guild_api:
            return
        self._fetch_server_blob(blob_id, srv, "icons", "icon_blob",
                                "icon_path")

    def _ingest_banner(self, blob_id, srv):
        """whoami guild banner (a blob id) -> blobs fetch ->
        banners/<host>_<blob head>.<ext> -> servers entry -> the sidebar
        strip refresh. Engine shared with the rail icon (the wrappers
        keep the whoami flow readable)."""
        self._fetch_server_blob(blob_id, srv, "banners", "banner_blob",
                                "banner_path")

    def _repair_recorded_assets(self, srv):
        """Self-heal recorded blob assets after an upgrade: the pre-
        assembly ingest wrote the FIRST SLICE as the whole file, so any
        asset over the slice cap landed truncated (and possibly mis-
        named) while the record still looked valid - the unchanged check
        then kept binding the junk forever (the device banner defect).
        Re-ingest when the stored bytes disagree with the blob's total
        size or the sniffed format."""
        if not srv or not self.guild_api:
            return
        for blob_key, path_key, subdir, ingest in (
                ("icon_blob", "icon_path", "icons",
                 self._ingest_guild_icon),
                ("banner_blob", "banner_path", "banners",
                 self._ingest_banner)):
            blob_id = srv.get(blob_key)
            path = srv.get(path_key)
            if not blob_id or not path:
                continue
            if not os.path.isfile(path):
                print("asset repair (%s): record lost its file, reingesting"
                      % subdir)
                srv.pop(blob_key, None)
                srv.pop(path_key, None)
                self._fetch_server_blob(blob_id, srv, subdir, blob_key,
                                        path_key)
                continue
            try:
                res = self.guild_api.blobs_get(blob_id, offset=0)
                raw = base64.b64decode(res.get("data_b64") or b"")
            except Exception as e:
                print("asset repair (%s): probe failed" % subdir, repr(e))
                continue
            if not res.get("ok") or not raw:
                continue
            ext, _renderable = _blob_sniff(raw)
            stored_ext = os.path.splitext(path)[1].lstrip(".").lower()
            stored = os.path.getsize(path)
            if stored < (res.get("total") or 0) or (
                    ext != "bin" and stored_ext != ext):
                print("asset repair (%s): stored %d of %s bytes, ext %s "
                      "(blob says %s) -> reingesting" % (
                          subdir, stored, res.get("total"), stored_ext,
                          ext))
                srv.pop(blob_key, None)
                srv.pop(path_key, None)
                self._fetch_server_blob(blob_id, srv, subdir, blob_key,
                                        path_key)

    def _fetch_server_blob(self, blob_id, srv, subdir, blob_key, path_key):
        """Shared ingest engine for server-owned blob assets: fetch a
        blob by id (assembled across the chunked transport, not a single
        1 MiB slice), store it under _app_data_dir()/subdir as
        <host>_<blob head>.<ext> only when the format decodes, record
        blob_key/path_key on the servers entry, then refresh the
        sidebar. Disk writes happen on the caller worker thread; only
        the widget refresh hops to the main thread."""
        if not blob_id or not srv or not self.guild_api:
            return
        if srv.get(blob_key) == blob_id and srv.get(path_key):
            return  # unchanged
        api = self.guild_api

        def work():
            try:
                res = api.blobs_get_all(blob_id)
            except Exception as e:
                print("asset fetch failed (%s):" % subdir, repr(e))
                return
            if not res.get("ok"):
                print("asset fetch refused (%s):" % subdir,
                      repr(res.get("error")))
                return
            import base64 as _b64
            raw = _b64.b64decode(res.get("data_b64") or b"")
            if not raw:
                return
            ext, renderable = _blob_sniff(raw)
            print("asset ingest (%s): %d bytes, format %s%s" % (
                subdir, len(raw), ext,
                "" if renderable
                else "; format not renderable, letter tile stays"))
            try:
                dest_dir = os.path.join(_app_data_dir(), subdir)
                os.makedirs(dest_dir, exist_ok=True)
                host = (srv.get("host") or "server").replace(":", "_")
                if not renderable:
                    srv[blob_key] = blob_id
                    self._save_servers()
                    Clock.schedule_once(
                        lambda dt: self._refresh_server_sidebar(), 0)
                    return
                path = os.path.join(
                    dest_dir,
                    "%s_%s.%s" % (host, blob_id[:12], ext))
                with open(path, "wb") as f:
                    f.write(raw)
                srv[blob_key] = blob_id
                srv[path_key] = path
                self._save_servers()
                Clock.schedule_once(
                    lambda dt: self._refresh_server_sidebar(), 0)
            except Exception as e:
                print("asset save failed (%s):" % subdir, repr(e))
        threading.Thread(target=work, daemon=True).start()

    def _select_server(self, idx):
        """Switch to a different server."""
        if idx == self._active_server_idx:
            return
        self._set_active_server(idx)
        self._update_mesh(False)
        self._refresh_server_sidebar()
        # Clear all channel/chat state for the new server
        self._channels.clear()
        self._channel_kinds.clear()
        self._channel_can_post.clear()
        self._channel_unread.clear()
        self._msg_widgets.clear()
        self._older.clear()
        self._seq = 0
        self._last_ts = 0.0
        self._stream_cid = None
        self._stream_cursor = None
        # cancel, don't drop: a reconnect re-schedules, and two intervals
        # would double every poll
        ev = getattr(self, "_stream_tick_event", None)
        if ev:
            ev.cancel()
        self._stream_tick_event = None
        self._site_stack.clear()
        self._site_current = None
        fwd = getattr(self, "_site_forward", None)
        if fwd is not None:
            fwd.clear()
        # sitenav (d): a server switch leaves every site page behind.
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            _refresh_nav(visible=False)
        self._site_theme.clear()
        self._term_sessions.clear()
        self._term_page = None
        # Clear UI
        try:
            main = self.root.get_screen("main")
            main.ids.channel_grid.clear_widgets()
            main.ids.message_grid.clear_widgets()
            main.ids.channel_header.text = "#general"
        except Exception:
            pass
        self._channels_added = False
        # Reconnect to the new server (H2: guarded + threaded like a
        # fresh connect -- the old Clock-scheduled _connect_after_rns
        # ran its ~150s of blocking sleeps on the main thread).
        if self._begin_connect():
            if self.reticulum is not None:
                self._start_connect_after_rns()
            else:
                Clock.schedule_once(lambda dt: self._init_reticulum(), 0)
        self._set_status("Switching to %s..." % self._servers[idx].get("display_name", "server"))

    @staticmethod
    def _wait_path_mtu(nh, tries=15):
        """Wait for a next-hop interface that exposes its hardware MTU.

        0.31.8 (boot.log receipt): a re-establish right after an interface
        abort ran against an invalidated path. RNS then signs the DEFAULT
        link MTU (500 B) instead of the interface's 8192, and the server
        confirms it ("Destination confirmed link MTU of 500 B"); resources
        fail on such links ("Sending request ... failed with status: 07").
        The hub re-announces every ~10 s, so a short bounded wait restores
        full-MTU links after every abort.

        Returns True when a live MTU-signalling path exists, False after
        `tries` seconds (callers proceed regardless -- never worse than
        the old unconditional establish).
        """
        for _ in range(tries):
            if RNS.Transport.next_hop_interface_hw_mtu(nh) is not None:
                return True
            time.sleep(1.0)
        return False

    def _establish_guild_link(self):
        """Establish the RNS link to the guild node; up to 3 attempts.

        Extracted from _init_reticulum (v0.31.2) so boot and the
        link self-heal both go through the identical routine: fresh
        destination from the recalled hub identity, 20s wait for
        ACTIVE per attempt, identify, closed-callback registration.
        Raises RuntimeError when no attempt goes ACTIVE.
        """
        nh = bytes.fromhex(self.guild_node_hash)
        nid = RNS.Identity.recall(nh)
        if nid is None:
            raise RuntimeError(
                "RNS does not know identity for node_hash %s — "
                "the guild's announce was not heard yet. Try again." % self.guild_node_hash)
        self._wait_path_mtu(nh)
        link = None
        for attempt in range(3):
            nd = RNS.Destination(nid, RNS.Destination.OUT,
                                 RNS.Destination.SINGLE, "nomadnetwork", "node")
            link = RNS.Link(nd)
            for _ in range(200):
                if link.status == RNS.Link.ACTIVE:
                    break
                time.sleep(0.1)
            if link.status == RNS.Link.ACTIVE:
                break
            print(f"Link attempt {attempt + 1} not ACTIVE, retrying...")
            time.sleep(1.0)

        if link.status != RNS.Link.ACTIVE:
            raise RuntimeError(
                "Could not establish RNS link to guild node — "
                "link never went ACTIVE after 3 attempts.")
        link.identify(self.client_identity)
        # v0.31.2: the teardown event is the fastest dead signal we get.
        try:
            link.set_link_closed_callback(self._on_guild_link_closed)
        except Exception as e:
            print("link closed-callback registration failed: %s" % e)
        return link

    def _on_guild_link_closed(self, link, reason=None):
        print("[link] guild link closed (%r) — scheduling re-establish" % (reason,))
        Clock.schedule_once(lambda dt: self._update_mesh(False), 0)
        Clock.schedule_once(self._do_reestablish, 1.0)

    def _on_guild_link_dead(self, state=None):
        # Request-path signal (GuildAPI saw a non-ACTIVE link or a raise).
        print("[link] guild link dead (%r) — scheduling re-establish" % (state,))
        Clock.schedule_once(lambda dt: self._update_mesh(False), 0)
        Clock.schedule_once(self._do_reestablish, 0.5)

    def _update_mesh(self, active=None):
        """Read the known RNS path. Unknown hop count stays a generic mesh link."""
        if active is None:
            active = (self.link is not None
                      and getattr(self.link, "status", None) == RNS.Link.ACTIVE)
        hops = None
        if active and self.guild_node_hash:
            try:
                hops = RNS.Transport.hops_to(bytes.fromhex(self.guild_node_hash))
            except (AttributeError, TypeError, ValueError):
                pass
        self.mesh_mode, self.mesh_label, self.mesh_hops = link_state(active, hops)

    def _do_reestablish(self, dt=None):
        """One guarded re-establish attempt; any caller can schedule it.

        Idempotency: concurrent triggers (request-path on_dead, link
        closed callback, keepalive zombie counter) all funnel here;
        the flag guarantees a single in-flight attempt and a failure
        reschedules once. On success the fresh link replaces the old
        one everywhere (self.link, GuildAPI, push callback) and the
        stream subscription is re-primed from the watermark (the
        server binds one subscription per link; a new link has none).

        H2: the blocking work runs on a daemon worker, not the main
        thread -- _establish_guild_link sleeps up to 60s and the old
        Clock-scheduled call froze the UI for the whole attempt.
        """
        if getattr(self, "_reestablishing", False):
            return
        self._reestablishing = True
        threading.Thread(target=self._do_reestablish_work,
                         daemon=True).start()

    def _do_reestablish_work(self):
        """Worker half of _do_reestablish (see its docstring)."""
        try:
            old = getattr(self, "link", None)
            if old is not None and getattr(old, "status", None) == RNS.Link.ACTIVE:
                # Someone already restored an ACTIVE link (or the signal
                # was stale); only a non-ACTIVE link needs replacing.
                Clock.schedule_once(
                    lambda dt2: self._start_streamer(), 0)
                return
            print("[link] re-establishing guild link...")
            try:
                old.teardown()
            except Exception:
                pass
            link = self._establish_guild_link()
            self.link = link
            self.guild_api = GuildAPI(self.reticulum, link,
                                      on_dead=self._on_guild_link_dead,
                                      log=dlog)
            try:
                link.set_packet_callback(self._on_push_frame)
            except Exception as e:
                print("push callback registration failed: %s" % e)
            print("[link] guild link re-established")
            Clock.schedule_once(lambda dt2: self._start_streamer(), 0)
            Clock.schedule_once(lambda dt2: self._update_mesh(), 0)
        except Exception as e:
            print("[link] re-establish failed: %r — retrying in 8s" % (e,))
            Clock.schedule_once(self._do_reestablish, 8.0)
        finally:
            self._reestablishing = False

    def _init_reticulum(self):
        """Main thread only: create RNS instance (signal handlers), then
        spawn a background thread for the blocking link establishment."""
        # H2: never rebuild RNS while a connect worker is alive, and
        # never rebuild it at all once it exists -- the retry path goes
        # straight to the worker.
        _worker = getattr(self, "_connect_worker", None)
        if _worker is not None and _worker.is_alive():
            return
        if self.reticulum is not None:
            self._start_connect_after_rns()
            return
        if BOOT_DEBUG:
            _boot_mark("RNS: writing config")
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            os.makedirs(storage, exist_ok=True)

            # Write an RNS config that routes to the guild. If a hub address was
            # provided, use a TCPClientInterface to reach it directly;
            # otherwise use AutoInterface for LAN multicast discovery.
            # H1: never hand RNS a busy listen port -- its bind failure
            # panics the process (os._exit(255)) past every try/except.
            # Probe first; a held 29222 falls back to 29222+N.
            self.client_port = _free_client_port(29222)
            cfgfile = os.path.join(storage, "config")
            if not os.path.exists(cfgfile) or self._config_needs_update(cfgfile):
                with open(cfgfile, "w") as f:
                    # enable_transport = no: the client is a leaf endpoint, not
                    # a transport node. With transport enabled, RNS treats us
                    # as a router and rebroadcasts the guild's announces; the
                    # per-interface announce rate limiter then queues those
                    # rebroadcasts instead of sending them, and every queued
                    # one makes RNS log "No interfaces could process the
                    # outbound packet". The guild announces itself every 10s
                    # already, so the client gains nothing by forwarding it.
                    f.write("[reticulum]\n"
                            "  enable_transport = no\n"
                            "  share_instance = no\n\n"
                            "[interfaces]\n")
                    if self.hub_address:
                        f.write("  [[TCPClientInterface]]\n"
                                "    type = TCPClientInterface\n"
                                "    enabled = yes\n"
                                "    target_host = %s\n"
                                "    target_port = %d\n" % (self.hub_host, self.hub_port))
                    else:
                        f.write("  [[AutoInterface]]\n"
                                "    type = AutoInterface\n"
                                "    enabled = yes\n")

                    # ingress_control = no: the client is a leaf that talks to
                    # exactly one guild. RNS's ingress limiter holds announces
                    # for destinations it does not know yet, and the guild's
                    # nomadnetwork.node announce is exactly such a destination
                    # until the client discovers it -- so the limiter can
                    # swallow the very announce discovery is waiting for,
                    # making the connect fail intermittently. We only ever
                    # talk to the one hub we were pointed at, so there is no
                    # announce flood to defend against.
                    f.write("    ingress_control = no\n")

                    f.write("  [[TCPServerInterface]]\n"
                            "    type = TCPServerInterface\n"
                            "    enabled = yes\n"
                            "    listen_ip = 0.0.0.0\n"
                            "    listen_port = %d\n" % self.client_port)
                    # Same reasoning as the client interface above: other
                    # clients' lxmf.delivery announces arrive here as
                    # unknown destinations and would otherwise be held by
                    # the ingress limiter.
                    f.write("    ingress_control = no\n")

            from RNS.vendor.platformutils import is_android as _is_android

            def _android_diag_default():
                # Diag builds: RNS debug verbosity ON by default on-device.
                # buildozer set_env may not survive into the app's runtime
                # environment, so the platform decides, not the env var;
                # RH_DEBUG=1 still forces it anywhere, RH_DEBUG=0 silences.
                return _is_android() and os.environ.get("RH_DEBUG") is None

            debug_setting = os.environ.get("RH_DEBUG", "").strip().lower()
            debug_enabled = (debug_setting in ("1", "true", "yes") or
                             (not debug_setting and _android_diag_default()))

            reticulum = RNS.Reticulum(
                configdir=storage,
                loglevel=RNS.LOG_DEBUG if debug_enabled else None)
            self.reticulum = reticulum
            if BOOT_DEBUG:
                _boot_mark("RNS: instance up (config ok)")
        except Exception as e:
            print("init reticulum error:", repr(e))
            self._set_status("connect failed: %s" % e)
            # H2: no worker was spawned, so release the slot claimed by
            # _begin_connect -- otherwise every later connect is ignored.
            self._connect_in_flight = False
            return
        # Identity and the LXMRouter must be built on the MAIN thread --
        # see _init_identity_and_lxmf. Only the blocking link work below
        # moves to a worker.
        self._init_identity_and_lxmf()
        if getattr(self, "_identity_damaged", False):
            # H3: the stored identity is corrupt -- do not connect with
            # a minted replacement. Release the connect slot and park
            # on the connect screen with the recovery message.
            self._connect_in_flight = False
            msg = ("Identity damaged — restore your recovery kit "
                   "or start fresh")
            Clock.schedule_once(
                lambda dt: self._return_to_connect_screen(msg), 0)
            return
        if BOOT_DEBUG:
            _boot_mark("identity + LXMF router ok, link worker starting")
        self._start_connect_after_rns()

    def _config_needs_update(self, cfgfile):
        """Check if the stored RNS config matches the current hub address."""
        try:
            with open(cfgfile) as f:
                content = f.read()
            # Configs written by older builds used enable_transport = yes.
            # Rewrite those: running as a transport node makes RNS queue the
            # guild's rebroadcast announces and log "No interfaces could
            # process the outbound packet". See _init_reticulum.
            if "enable_transport = yes" in content:
                return True
            # Both the host AND the port must be known before the stored
            # config can be compared -- a session can be missing either one,
            # and "%d" % None raises TypeError.
            if self.hub_address and self.hub_host and self.hub_port:
                if ("target_host = %s" % self.hub_host) not in content or \
                        ("target_port = %d" % int(self.hub_port)) not in content:
                    return True
            elif "AutoInterface" not in content:
                return True
            # H1: the listen port is part of the config too -- a stale
            # file holding the old (now busy) port must be rewritten
            # with the probed fallback, or RNS would bind the busy one.
            port = getattr(self, "client_port", 29222)
            return ("listen_port = %d" % port) not in content
        except Exception:
            return True

    def _hub_tcp_connected(self):
        """TCP link state to our hub: True when the TCPClientInterface
        to (hub_host, hub_port) is online, False when it exists but
        never connected, None when there is no such interface to judge
        (AutoInterface mode, or no hub configured).

        H7a: RNS only logs a refused endpoint to stdout ([Errno 111]);
        the interface's `online` flag is the programmatic signal.
        """
        try:
            if not self.hub_host or not self.hub_port:
                return None
            for iface in RNS.Transport.interfaces:
                if getattr(iface, "target_ip", None) is None:
                    continue
                if (str(getattr(iface, "target_ip", "")) ==
                        str(self.hub_host)
                        and int(getattr(iface, "target_port", -1)) ==
                        int(self.hub_port)):
                    return bool(getattr(iface, "online", False))
            return None
        except Exception:
            return None

    def _hub_unreachable_error(self):
        """H7a: the fault is the hub endpoint, not node_hash discovery."""
        return ("Could not reach hub %s (connection refused / "
                "unreachable). Check the hub is running and the "
                "address is right." % (self.hub_address or ""))

    def _discover_node_hash(self, reticulum):
        """Listen for RNS announces from the hub's nomadnetwork.node destination.

        When a hub announces, its announce packet carries the destination hash
        and public key. Our announce handler captures the hash; RNS populates
        known_destinations from the announce body, so Identity.recall() then
        returns the peer identity.
        Returns the node_hash as hex string, or None on timeout.
        """
        discovered = {}

        class _NodeAnnounceHandler:
            aspect_filter = "nomadnetwork.node"

            def received_announce(self, destination_hash, announced_identity, app_data):
                if isinstance(destination_hash, bytes):
                    discovered["hash"] = destination_hash.hex()
                else:
                    discovered["hash"] = str(destination_hash)

        handler = _NodeAnnounceHandler()
        RNS.Transport.register_announce_handler(handler)

        try:
            # Let the TCPClientInterface establish its connection to the
            # community hub before listening for announces.
            time.sleep(2.0)
            # Guild re-announces every 10 seconds; wait up to 60 seconds
            # to hear one after connecting.
            for _ in range(600):  # up to 60 seconds
                if "hash" in discovered:
                    return discovered["hash"]
                time.sleep(0.1)
        finally:
            # Handler stays registered; harmless for subsequent announces.
            pass

        return None

    _up_active = False  # an attachment/blob upload is in flight (set at both
    # blobs_put call sites); while true, keepalive misses are ignored --
    # an upload monopolizes the priority-agnostic link FIFO and legitimately
    # delays the poll's receipt past its deadline (manual receipt: RNS is
    # priority-agnostic on general traffic). Two counting misses here would
    # tear the link MID-UPLOAD (v0.31.2 self-inflicted; receipted this
    # session) -- this gate removes that path entirely.
    _DMA_INTERVAL = 300.0  # seconds between client lxmf.delivery re-announces

    def _init_lxmf_router(self):
        """Build/repair the LXMF router on the existing client identity.

        Extracted (v0.31.5) from the boot path so the resume heal can run
        the identical sequence: Android STOPS the activity for every SAF
        file pick (opaque picker, OS-mandated), and on_stop historically
        exited the router mid-session there (receipted). Boot failure also
        retries here on every resume instead of disabling DMs for good.
        """
        if self.client_identity is None:
            return
        if self.lxmf_router is not None and self.lxmf_delivery is not None:
            # 0.31.8 (boot.log receipt): the resume heal fires exactly when
            # tay returns from a SAF pick -- while the FIRST router is still
            # healthy. Rebuilding then raised KeyError('Attempt to register
            # an already registered destination.') and the except nulled the
            # router: the healer itself disabled DMs for the rest of the
            # session, on every pick. A healthy router needs no repair.
            return
        try:
            import LXMF
            router_was_none = self.lxmf_router is None
            self.lxmf_router = LXMF.LXMRouter(
                identity=self.client_identity,
                storagepath=os.path.join(_app_data_dir(), "lxmf"))
            self.lxmf_delivery = self.lxmf_router.register_delivery_identity(
                self.client_identity,
                display_name=(self.username or "sHades"))
            self.lxmf_router.register_delivery_callback(self._on_dm_delivery)
            # announce (re)so friends can hear our lxmf.delivery
            self.lxmf_router.announce(self.lxmf_delivery.hash)
            if router_was_none:
                threading.Thread(
                    target=self._dm_announce_loop, daemon=True).start()
        except Exception as e:
            print("LXMF router init failed (DMs disabled):", repr(e))
            self.lxmf_router = None
            self.lxmf_delivery = None

    def _heal_after_resume(self):
        """v0.31.5 resume heal (tay: 'We don't *want* to stop the app.

        That is literally the blocking behavior'). Android stops the
        activity for every SAF pick; we cannot prevent the stop, so the
        return must be seamless: reset the keepalive counter (it froze
        mid-stop), rebuild the LXMF router if on_stop exited it, and
        re-establish a dead guild link. Chat must work immediately after
        every pick, without a restart.
        """
        try:
            self._ka_fail = 0
            self._init_lxmf_router()
            api = self.guild_api
            if api is not None:
                link = getattr(api, "link", None)
                if link is not None and getattr(link, "status", None) \
                        not in (None, RNS.Link.ACTIVE):
                    Clock.schedule_once(self._do_reestablish, 0)
        except Exception as e:
            print("resume heal error:", repr(e))

    def _announce_dm_once(self):
        if self.lxmf_router and self.lxmf_delivery:
            try:
                self.lxmf_router.announce(self.lxmf_delivery.hash)
            except Exception as e:
                print("announce failed:", repr(e))
        return True

    def _dm_announce_loop(self):
        # Runs on a daemon thread started by _init_identity_and_lxmf; the
        # router and delivery destination persist across reconnects, so
        # the loop only dies with the process. See v0.31.3 comment there.
        while True:
            time.sleep(self._DMA_INTERVAL)
            self._announce_dm_once()

    def _init_identity_and_lxmf(self):
        """Main thread only: build the client identity, the lxmf.delivery
        destination and the LXMRouter.

        LXMRouter's constructor installs SIGINT/SIGTERM handlers through
        signal.signal(), and Python only allows signal.signal() on the
        main thread of the main interpreter. Building the router from a
        worker thread raised
        ValueError('signal only works in main thread of the main
        interpreter'), which the old code swallowed -- so DMs were
        permanently dead and every run printed a scary error.

        Called once per identity; switching servers keeps the same
        identity and router (both are client-local, not per-guild).
        """
        if self.client_identity is not None:
            return
        self._identity_damaged = False
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            ipath = os.path.join(storage, "identity")
            if os.path.exists(ipath):
                try:
                    self.client_identity = RNS.Identity.from_file(ipath)
                except Exception:
                    self.client_identity = None
                if self.client_identity is None:
                    # H3: corrupted or empty identity file -- DO NOT mint
                    # a fresh one. That silently changed my_hash and the
                    # user permanently lost their identity, username
                    # claim, DMs and roles. This is the boot equivalent
                    # of _ensure_identity_file's ValueError("identity is
                    # damaged; restore a recovery kit"): stop the
                    # connect (see _init_reticulum) and leave the
                    # damaged file untouched for recovery.
                    self._identity_damaged = True
                    return
            else:
                self.client_identity = RNS.Identity()
                self.client_identity.to_file(ipath)

            # client delivery destination (for identification + LXMF)
            self.client_dest = RNS.Destination(
                self.client_identity, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery")
            self.my_hash = hexstr(self.client_dest.hash)
            # Now that the lxmf.delivery hash exists, adopt the same
            # unique fallback name the guild would assign. A client that
            # has never chosen a username must not sit on the literal
            # "user": the first one to save it takes it for good and
            # every later client gets 409 "username taken".
            if self.username in ("", "user", None):
                self.username = default_username(self.my_hash)

            # Direct client-to-client DM transport (U7): a real LXMRouter on
            # our identity. Inbound DMs arrive at our lxmf.delivery destination
            # (nothing else ever does — guild chat rides the API link), so the
            # delivery callback IS the DM inbox. Failure to init just
            # disables DMs; chat keeps working.
            self._init_lxmf_router()
        except Exception as e:
            print("identity init error:", repr(e))
            self.client_identity = None
            self.client_dest = None
            self.my_hash = None

    def _connect_after_rns(self):
        """Background thread: link to guild node.

        The identity and the LXMRouter are already built (on the main
        thread, by _init_identity_and_lxmf); only the blocking link
        establishment happens here.
        """
        try:
            reticulum = self.reticulum

            # Discover guild node_hash via RNS announce if not provided.
            # The hub announces nomadnetwork.node over the TCPClientInterface
            # we just connected through; we capture the hash from the announce.
            if not self.guild_node_hash:
                # H7a: a refused/unreachable TCP endpoint can never
                # deliver an announce -- fail fast with the real fault
                # instead of waiting 60s and then blaming node_hash
                # discovery. The interface retries every 5s, so a
                # ~12s poll covers two connect attempts before
                # concluding the endpoint is down.
                if self.hub_address and \
                        self._hub_tcp_connected() is False:
                    for _ in range(24):
                        time.sleep(0.5)
                        if self._hub_tcp_connected() is not False:
                            break
                    if self._hub_tcp_connected() is False:
                        raise RuntimeError(self._hub_unreachable_error())
                self.guild_node_hash = self._discover_node_hash(reticulum)
                if not self.guild_node_hash:
                    if self._hub_tcp_connected() is False:
                        raise RuntimeError(self._hub_unreachable_error())
                    raise RuntimeError(
                        "Could not discover guild node_hash from hub announces. "
                        "Ask the hub admin for its node_hash and try again.")
                print(f"Discovered guild node_hash via announce: {self.guild_node_hash}")

            # Validate that RNS knows this identity (from hearing its announce)
            self.node_hash = self.guild_node_hash
            nh = bytes.fromhex(self.guild_node_hash)
            nid = RNS.Identity.recall(nh)
            if nid is None:
                # Boot-race fix (v0.33.9, tay's t_4 launches 2/3): a resumed
                # boot carries guild_node_hash in the saved session and the
                # discovery wait is skipped, so the recall ran ~2s after TCP
                # came up — before the hub's announce landed (the guild
                # re-announces every 10s). One failed recall parked the
                # client on the connect screen until the user killed it.
                # Instead: wait for the announce we need, up to 30s
                # (three re-announce windows); only then fail.
                print("connect: identity not known yet; waiting for the "
                      "hub's announce (re-announces every 10s, up to 30s)")
                for _ in range(60):
                    time.sleep(0.5)
                    nid = RNS.Identity.recall(nh)
                    if nid is not None:
                        break
            if nid is None:
                # H7a: no announce can arrive over a dead TCP link --
                # say so instead of blaming the announce wait.
                if self._hub_tcp_connected() is False:
                    raise RuntimeError(self._hub_unreachable_error())
                raise RuntimeError(
                    "RNS does not know identity for node_hash %s — "
                    "the guild's announce was not heard within 30s. "
                    "Try again." % self.guild_node_hash)
            print(f"RNS recalled identity for node_hash {self.guild_node_hash}")

            # link to guild node -- extracted so the self-heal path
            # (v0.31.2) re-establishes through the exact same routine.
            link = self._establish_guild_link()
            self.link = link
            self.guild_api = GuildAPI(self.reticulum, link,
                                      on_dead=self._on_guild_link_dead,
                                      log=dlog)
            Clock.schedule_once(lambda dt: self._update_mesh(), 0)
            # v0.28 push: stream frames arrive on this link the moment
            # events exist. The callback is the client's entire intake;
            # no polling loops exist anywhere in the client.
            try:
                link.set_packet_callback(self._on_push_frame)
            except Exception as e:
                print("push callback registration failed: %s" % e)
            self._save_session()

            # Create this client's own nomadnetwork.node destination
            # for serving profile pages to other clients.
            self.node_destination = RNS.Destination(
                self.client_identity, RNS.Destination.IN,
                RNS.Destination.SINGLE, "nomadnetwork", "node")
            self.node_destination.register_request_handler(
                "/page/profile", self.page_profile_handler,
                allow=RNS.Destination.ALLOW_ALL)
            self.node_destination.register_request_handler(
                "/file", self._file_handler,
                allow=RNS.Destination.ALLOW_ALL)
            self.node_hash = hexstr(self.node_destination.hash)
            print(f"My node_hash: {self.node_hash}")

            # Register the node's DESTINATION HASH with the guild directory
            # so profiles / the node index can reach me cryptographically
            # (Identity.recall + RNS.Link). v0.29: ONE SHOT at connect (and
            # on reconnect). Presence is not this call: the guild derives it
            # from link liveness, which RNS monitors with keepalives/teardowns.
            try:
                self.guild_api.register_node(self.node_hash)
                print(f"Registered with guild: node {self.node_hash[:12]}")
            except Exception as e:
                print(f"Node registration failed: {e}")

            Clock.schedule_once(lambda dt: self._switch_main(), 0)
            Clock.schedule_once(lambda dt: self._start_poll(), 0)
            Clock.schedule_once(lambda dt: self._start_streamer(), 0)
            Clock.schedule_once(lambda dt: self._start_stream_keepalive(), 0)
            Clock.schedule_once(lambda dt: self._refresh_channels_from_guild(), 1.0)
            Clock.schedule_once(lambda dt: self._fetch_whoami(), 1.2)
            # join gate (U6b): if the guild requires an invite code, prompt
            Clock.schedule_once(lambda dt: self._check_join_gate(), 1.6)
        except Exception as e:
            print("connect guild error:", repr(e))
            err_msg = "connect failed: %s" % e
            Clock.schedule_once(
                lambda dt: self._set_status(err_msg), 0)
            # Go back to the connect screen so the user can retry or point
            # the client at a different guild. Without this a failed resume
            # leaves the user stranded on an empty main screen.
            Clock.schedule_once(
                lambda dt: self._return_to_connect_screen(err_msg), 0)

    def _switch_main(self):
        self.root.current = "main"
        self._update_mesh()
        # load channels (placeholder: general + anything discovered)
        self._refresh_channels_ui()
        self._refresh_server_sidebar()
        # Ensure self is in member list
        if self.my_hash and self.username:
            self._record_member(self.my_hash, self.username)
        # start streaming the default channel right away — otherwise the
        # streamer idles until the first manual channel switch
        name = "general"
        try:
            name = getattr(self.root.get_screen("main"), "_current_channel", "general")
        except Exception:
            pass
        self._stream_cid = self._channel_cid(name)
        self._stream_cursor = None

    def _ka_miss(self):
        # Uploads in flight: no counting (see _up_active note). A busy link
        # during a transfer is not a dead link; re-establishing here would
        # tear the link under our own transfer.
        if self._up_active:
            print("[link] keepalive missed, upload in flight -- not counting")
            self._ka_fail = 0
            return
        misses = getattr(self, "_ka_fail", 0) + 1
        self._ka_fail = misses
        print("[link] keepalive miss %d/2" % misses)
        if misses >= 2:
            self._ka_fail = 0
            Clock.schedule_once(self._do_reestablish, 0)

    def _fetch_messages(self, channel, since_id=None, since_ts=None,
                        before=None, limit=50, prepend=False):
        # site/term channels don't have a message ledger — their grid is a
        # rendered micron page; never stomp it with chat fetches
        if self._channel_kinds.get(self._channel_cid(channel), "chat") \
                not in ("chat", "announce"):
            return
        # Network call must not block the main thread — run it in background,
        # schedule UI updates via Clock.schedule_once.

        def work():
            cid = self._channel_cid(channel)
            res = self.guild_api.messages(cid, since_id=since_id, since_ts=since_ts,
                                          before=before, limit=limit)
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("fetch failed: %s" % err), 0)
                Clock.schedule_once(
                    lambda dt: self._remove_skeleton(cid), 0)
                return
            msgs = res.get("messages", []) or []
            try:
                guild = self._cache_guild()
                if guild:
                    with self._cache_write_lock:
                        offline_store.put(
                            self._offline_path(), guild, cid,
                            [m for m in msgs
                             if not self._post_was_purged(guild, m)])
            except Exception as exc:
                print("offline cache write failed:", repr(exc))
            updates = res.get("updates", []) or []
            # reading the channel: mark the newest message read (never for
            # pagination prepends — those are older than the watermark)
            if msgs and not prepend and not since_ts:
                newest = msgs[-1].get("msg_id")
                if newest:
                    try:
                        self.guild_api.receipts(cid, newest)
                    except Exception as e:
                        print("receipt send failed:", repr(e))

            def apply(dt):
                try:
                    main = self.root.get_screen("main")
                    grid = main.ids.message_grid
                    self._remove_skeleton(cid)
                    # H6: a later success clears a stale fetch-failure
                    # status -- the old code only ever set it. getattr:
                    # minimal test stubs bind _fetch_messages without
                    # the rest of the app; a missing helper must not
                    # break rendering.
                    _clear = getattr(self, "_clear_fetch_status", None)
                    if _clear is not None:
                        _clear()
                    if prepend:
                        if not msgs:
                            # Server has no older pages; stop trying.
                            st = self._older.setdefault(
                                cid, {"oldest_id": None, "has_older": False,
                                      "loading": False})
                            st["loading"] = False
                            return
                        # Scroll-triggered pagination: insert older messages
                        # at the top of the grid while keeping the viewport
                        # anchored on the content that was already visible.
                        scroll = main.ids.scroll
                        viewport_h = scroll.height
                        old_content = grid.height
                        old_sy = scroll.scroll_y
                        offset = (1 - old_sy) * max(0, old_content - viewport_h)
                        for m in reversed(msgs):
                            self._render_message(grid, m)

                        def restore(dt2):
                            new_content = grid.height
                            span = max(0, new_content - viewport_h)
                            sy = 1.0 - offset / span if span > 0 else 1.0
                            try:
                                scroll.scroll_y = min(1.0, max(0.0, sy))
                            except Exception:
                                pass
                        Clock.schedule_once(restore, 0)
                        st = self._older.setdefault(
                            cid, {"oldest_id": None, "has_older": True,
                                  "loading": False})
                        st["oldest_id"] = msgs[0]["msg_id"]
                        st["has_older"] = len(msgs) == limit
                        st["loading"] = False
                    else:
                        # Live/delta fetch only: since_id/since_ts set
                        # means these rows arrived after the last render,
                        # so they fade in. The initial bulk load (neither
                        # set) renders at full opacity.
                        live = bool(since_id or since_ts)
                        for m in msgs:
                            if m.get("edited_at"):
                                row = self._msg_widgets.get(m.get("msg_id"))
                                if row is not None:
                                    # cached copy rendered first: the server
                                    # row is newer, refresh its content
                                    self._do_update(row, m.get("content") or "",
                                                    False)
                            _n = len(grid.children)
                            try:
                                self._render_message(grid, m, animate=live)
                            except TypeError:
                                # Minimal test stubs bind _render_message
                                # as (grid, m) with no animate kwarg
                                # (test_offline_cache.py); the real method
                                # and recording stubs accept it. Retry
                                # plain only if the failed call added no
                                # row, so a genuine internal TypeError is
                                # never retried into a duplicate.
                                if len(grid.children) == _n:
                                    self._render_message(grid, m)
                                else:
                                    raise
                        self._regroup_messages(grid)
                        Clock.schedule_once(
                            lambda dt: self._scroll_to_bottom(), 0.1)
                    # H7b: a fetch that returns nothing leaves an empty
                    # grid -- show the placeholder (a later render or
                    # push removes it via _render_message). getattr: see
                    # the _clear_fetch_status call above.
                    _refresh_ph = getattr(
                        self, "_refresh_empty_placeholder", None)
                    if _refresh_ph is not None:
                        _refresh_ph(grid)
                    for u in updates:
                        self._apply_update(u)
                except Exception as e:
                    print("render messages error:", e)
            Clock.schedule_once(apply, 0)
        threading.Thread(target=work, daemon=True).start()

    def _show_skeleton(self, cid):
        main = self.root.get_screen("main")
        grid = main.ids.message_grid
        self._skeleton_cid = cid
        self._skeleton_rows = [SkeletonRow() for _ in range(4)]
        for row in self._skeleton_rows:
            grid.add_widget(row)

    def _remove_skeleton(self, cid):
        if getattr(self, "_skeleton_cid", None) != cid:
            return
        try:
            grid = self.root.get_screen("main").ids.message_grid
            for row in getattr(self, "_skeleton_rows", []):
                from kivy.animation import Animation
                Animation.cancel_all(row)
                if row.parent is grid:
                    grid.remove_widget(row)
        except Exception:
            pass
        self._skeleton_rows = []
        self._skeleton_cid = None

    def _on_scroll_y(self, value):
        """Scroll-triggered pagination: when the user reaches the top of the
        chat and older messages remain, load the next page and prepend it."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        name = getattr(main, "_current_channel", "general")
        cid = self._channel_cid(name)
        st = self._older.get(cid)
        if not st or st.get("loading") or not st.get("has_older"):
            return
        if value < 0.97:
            return
        oldest = st.get("oldest_id")
        if not oldest:
            return
        st["loading"] = True
        self._fetch_messages(name, before=oldest, limit=50, prepend=True)

    def _refresh_empty_placeholder(self, grid=None):
        """Show 'No messages yet' when the message grid holds no rows.

        H7b: an empty channel used to show a blank grid with no cue.
        The placeholder is removed as soon as a real row lands (via
        _render_message) or the grid is cleared for another channel;
        _regroup_messages skips non-MessageRow children, so the label
        never disturbs grouping.
        """
        try:
            main = self.root.get_screen("main")
            grid = grid or main.ids.message_grid
        except Exception:
            return
        has_rows = any(isinstance(c, MessageRow) for c in grid.children)
        ph = getattr(self, "_empty_placeholder", None)
        if has_rows:
            # NOTE: main.ids.* are WeakProxy objects, so parent
            # identity must use == (a proxy `is` never the widget).
            if ph is not None and ph.parent == grid:
                grid.remove_widget(ph)
            self._empty_placeholder = None
            return
        if ph is None:
            from kivy.uix.label import Label
            from kivy.metrics import dp, sp
            ph = Label(text="No messages yet",
                       color=self.theme_colors.get(
                           "text_dim", (0.57, 0.64, 0.71, 1)),
                       font_size=sp(14), size_hint_y=None, height=dp(40),
                       halign="center", valign="middle")
            ph.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            self._empty_placeholder = ph
        if ph.parent != grid:
            grid.add_widget(ph)

    def _render_message(self, grid, m, animate=False):
        msg_id = m.get("msg_id")
        if msg_id in self._msg_widgets:
            return
        author = m.get("author_hash", "")
        uname = m.get("username") or ("user-" + author[:6])
        self._record_member(author, uname)
        content = m.get("content") or ""
        reply_author, reply_excerpt, body = split_reply(content)
        ts = time.strftime("%H:%M", time.localtime(m.get("ts", 0)))
        is_own = (author == self.my_hash)

        # avatar
        avatar_source = ""
        if author and is_own and getattr(self, "my_avatar_small", None):
            # Use our own saved circular pfp immediately
            avatar_source = self.my_avatar_small
        else:
            key = "av:" + author
            path = IMAGE_CACHE.get(key)
            if path:
                avatar_source = path
            elif author and IMAGE_CACHE.begin_load(key):
                # fetch avatar — deduplicate in-flight requests (never for
                # empty authors: system rows like command notices)
                def _do_av(a=author, k=key):
                    try:
                        # H4: no guild_api while offline -- there is
                        # nothing to fetch from; skip instead of
                        # raising AttributeError in the worker thread.
                        if self.guild_api is None:
                            return
                        res = self.guild_api.avatar_get(a)
                        if res.get("ok") and res.get("avatar"):
                            IMAGE_CACHE.set_bytes(k, res["avatar"], "png")
                            Clock.schedule_once(lambda dt: self._refresh_avatar(k), 0)
                    finally:
                        IMAGE_CACHE.end_load(k)
                threading.Thread(target=_do_av, daemon=True).start()

        row = MessageRow(
            author_hash=author,
            username=uname,
            ts=ts,
            content=content,
            content_markup=md_to_kivy(body),
            avatar_source=avatar_source,
            msg_id=msg_id,
            is_own=is_own,
            reply_author=reply_author,
            reply_excerpt=reply_excerpt,
        )
        row._message_meta = {"author_hash": author, "ts": m.get("ts", 0)}
        self._msg_widgets[msg_id] = row
        grid.add_widget(row)
        if animate:
            # Polish group 2: live arrivals fade in; bulk history renders
            # at full opacity (animate defaults False). reveal() is
            # opacity-only, so layout never moves, and reduced motion
            # lands the row at opacity 1 at once.
            import ui_fx
            ui_fx.reveal(row)
        sHadesClientApp._regroup_messages(grid)

        # attachment embeds (files uploaded with the message)
        for att in m.get("attachments") or []:
            if isinstance(att, dict):
                kind = att.get("kind")
                if kind == ATT_SERVER:
                    # Server-stored: re-fetch bytes on demand (loading
                    # placeholder until received) — this is what survives a
                    # restart for server-hosted files.
                    self._fetch_attachment(row, m.get("msg_id"), att)
                    continue
                if kind == ATT_EPHEMERAL:
                    # Ephemeral (P2P): bytes are kept in a local file; read
                    # them back and render immediately.
                    local_path = att.get("local_path") or ""
                    if not local_path:
                        continue
                    try:
                        with open(local_path, "rb") as f:
                            att_blob = f.read()
                    except Exception:
                        continue
                    self._add_attachment_embed(
                        row, att.get("name") or "attachment", att_blob)
                    continue
                # Legacy server dict without a kind field: has a path to fetch.
                if "path" in att:
                    self._fetch_attachment(row, m.get("msg_id"), att)
                    continue
            try:
                att_name = att[0]
                att_blob = att[2]
            except Exception:
                continue
            if isinstance(att_blob, str):
                import base64
                att_blob = base64.b64decode(att_blob)
            self._add_attachment_embed(row, att_name, att_blob)

        # reaction summary line (tappable: toggles that emoji for me)
        self._set_row_reactions(msg_id, m.get("reactions") or [])
        # H7b: a rendered row dismisses the empty-channel placeholder.
        # getattr: minimal test stubs bind _render_message without the
        # rest of the app; a missing helper must not break rendering.
        _refresh_ph = getattr(self, "_refresh_empty_placeholder", None)
        if _refresh_ph is not None:
            _refresh_ph(grid)

    @staticmethod
    def _regroup_messages(grid):
        """Recompute after history prepends and live arrivals, including boundaries."""
        previous = None
        for row in reversed(grid.children):
            if not isinstance(row, MessageRow):
                previous = None
                continue
            meta = getattr(row, "_message_meta", None)
            row.grouped = grouped_with(previous, meta)
            previous = meta

    # -- reactions (phase 6) ----------------------------------------------
    # tay (task 8): "reactions use emojis". Unicode chips render the char
    # as-is; old rows carry ascii aliases and stay renderable (they show as
    # :name). The presets are the picker's face; the server accepts any
    # gated emoji (phase 1 unicode gate).

    def _set_row_reactions(self, msg_id, reactions):
        """Replace a row's reaction chips with the given summary. Safe to
        call with an empty list (removes the line)."""
        row = self._msg_widgets.get(msg_id)
        if not row:
            return
        container = row.ids.get("embed_container")
        if not container:
            return
        for w in getattr(row, "_reaction_widgets", []):
            try:
                container.remove_widget(w)
            except Exception:
                pass
        row._reaction_widgets = []
        if not reactions:
            return
        line = BoxLayout(orientation="horizontal", size_hint_y=None,
                         height=dp(22), spacing=dp(6))
        for e in reactions:
            if not isinstance(e, dict) or not e.get("emoji"):
                continue
            if ord(e["emoji"][0]) < 128:
                # legacy ascii alias row: render the old :name chips
                chip = ClickableLabel(
                    text=":%s %d" % (e["emoji"], e.get("count", 0)),
                    color=(0.75, 0.78, 0.85, 1), font_size=sp(12))
                chip.size_hint_x = None
                chip.width = dp(24 + 9 * len(e["emoji"]))
                chip.bind(on_press=lambda inst, m=msg_id, em=e["emoji"]:
                          self._toggle_reaction(m, em))
                line.add_widget(chip)
                continue
            chip = ClickableLabel(text="%s %d" % (e["emoji"],
                                                  e.get("count", 0)),
                                  color=(0.75, 0.78, 0.85, 1), font_size=sp(13))
            chip.size_hint_x = None
            chip.width = dp(34 + 12 * (e.get("count", 0) >= 10))
            chip.bind(on_press=lambda inst, m=msg_id, em=e["emoji"]:
                      self._toggle_reaction(m, em))
            line.add_widget(chip)
        plus = ClickableLabel(text="[+]", color=(0.5, 0.53, 0.6, 1), font_size=sp(12))
        plus.size_hint_x = None
        plus.width = dp(30)
        plus.bind(on_press=lambda inst, m=msg_id: self._open_emoji_picker(m))
        line.add_widget(plus)
        row._reaction_widgets.append(line)
        container.add_widget(line)

    def _toggle_reaction(self, msg_id, emoji):
        if not self.guild_api:
            return

        def work():
            try:
                self.guild_api.react(msg_id, emoji)
            except Exception as e:
                print("react failed:", repr(e))
        threading.Thread(target=work, daemon=True).start()

    def _apply_reaction_counts(self, msg_id, reactions):
        """Update a rendered row from a reaction event or fetched row."""
        self._set_row_reactions(msg_id, reactions or [])

    @staticmethod
    def _reactions_from_counts(msg_id, data):
        """Rebuild a reaction summary from a stream event's counts dict."""
        counts = data.get("counts") or {}
        return [{"emoji": e, "count": n, "users": []}
                for e, n in sorted(counts.items())]

    # -- search (phase 6) --------------------------------------------------

    ATT_MEDIA_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ATT_MEDIA_AUDIO = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus",
                       ".oga", ".mp2", ".aac")
    ATT_MEDIA_VIDEO = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".ogv")

    @staticmethod
    def _attachment_kind(name, mime=None):
        """Classify an attachment name (+optional mime) into
        image / audio / video / file. One decision site for embeds and
        for the player row; the send side (mimetypes) and the render side
        share it. Video is download-only for now (v0.30): embedded video
        playback needs ffpyplayer in the build or a native surface -- a
        deliberate follow-up, named for tay, not smuggled in."""
        n = (name or "").lower()
        ext = os.path.splitext(n)[1]
        if m := (mime or "").lower():
            if m.startswith("image/"):
                return "image"
            if m.startswith("audio/") or "ogg" in m:
                return "audio"
            if m.startswith("video/"):
                return "video"
            if m in ("application/ogg",):
                return "audio"
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return "image"
        if ext in (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus",
                   ".oga", ".mp2", ".aac"):
            return "audio"
        if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi", ".ogv"):
            return "video"
        return "file"

    def _refresh_avatar(self, key):
        # re-render rows that use this avatar (messages + sidebar rows)
        path = IMAGE_CACHE.get(key)
        if not path:
            return
        author = key[3:]  # strip "av:" prefix
        for row in list(self._msg_widgets.values()) + \
                list(self._member_widgets.values()):
            if row.author_hash == author:
                row.avatar_source = ""   # force Kivy to re-bind source
                row.avatar_source = path

            # No embed rebuild: embeds come only from message attachments
            # (RNS-fetched bytes rendered at post time), never from URLs in
            # the text, so a text edit cannot add or remove one.

    def _scroll_to_bottom(self):
        try:
            sv = self.root.get_screen("main").ids.scroll
            sv.scroll_y = 0
        except Exception:
            pass

    # -- channels / members ---------------------------------------------

    def _refresh_channels_from_guild(self):
        """Pull the channel index from the guild (v1 JSON): fills the sidebar
        with every channel and records each one's kind (chat/site/term)."""
        if not self.guild_api:
            return

        def work():
            res = self.guild_api.channels_list()
            if not res.get("ok"):
                return
            chans = res.get("channels") or []

            def apply(dt):
                for c in chans:
                    if not isinstance(c, dict):
                        continue
                    cid, name = c.get("id"), c.get("name")
                    if not cid or not name:
                        continue
                    self._channel_kinds[cid] = c.get("kind", "chat")
                    # P8: an old server without the stamp stays open
                    self._channel_can_post[cid] = bool(c.get("can_post", True))
                    self._channel_order[cid] = c.get("position") or 0
                    self._channel_pos[name] = c.get("position") or 0
                    if name not in self._channels:
                        self._channels[name] = cid
                        # H7c: a seed button for this name may already
                        # exist (offline navigation seeds general /
                        # announcements / random); adopt it instead of
                        # adding a duplicate button for the same channel.
                        if not self._has_channel_button(name):
                            self._add_channel(name)
                    else:
                        self._channels[name] = cid
                # H7c: drop seeds the guild does not know. Selecting
                # one the guild lacks used to give an empty grid plus
                # "fetch failed: unknown channel". Offline (no guild
                # list) the seeds stay -- they are needed for cache
                # navigation. Runs here, on the main thread, after the
                # add loop has populated _channels (so a removed
                # visible seed can fall back to a real channel).
                guild_names = {c.get("name") for c in chans
                               if isinstance(c, dict) and c.get("name")}
                for seed in ("general", "announcements", "random"):
                    if seed not in guild_names:
                        self._remove_channel_button(seed)
            self._channel_unread.update(
                {c["id"]: c.get("unread", 0) for c in chans
                 if isinstance(c, dict) and c.get("id")})
            Clock.schedule_once(lambda dt: self._update_channel_badges(), 0)
            Clock.schedule_once(apply, 0)
        threading.Thread(target=work, daemon=True).start()

    # whoami is the admin-menu gate; failure retries are bounded and
    # lane-tunable (never an unbounded loop)
    _WHOAMI_RETRY_N = 2
    _WHOAMI_RETRY_S = 4.0

    def _fetch_whoami(self):
        """Fetch our own identity/role view (gates moderator menu items)."""
        if not self.guild_api:
            return

        def work(tries_left=None):
            if tries_left is None:
                tries_left = self._WHOAMI_RETRY_N
            try:
                res = self.guild_api.whoami()
            except Exception as e:
                print("whoami failed:", repr(e))
                # bounded retry: the admin menu rides is_admin, which
                # only a successful whoami sets; one early failure must
                # not cost the panel for the whole session
                if tries_left > 0:
                    Clock.schedule_once(
                        lambda dt: work(tries_left - 1),
                        self._WHOAMI_RETRY_S)
                return
            try:
                from main import _export_bootlog_for_user
                _export_bootlog_for_user()
            except Exception:
                pass
            if res.get("ok"):
                self.i_am_moderator = bool(res.get("is_moderator"))
                self.i_am_admin = bool(res.get("is_admin"))
                self.my_user_hash = (res.get("user_hash") or "").lower()
                self.my_roles = res.get("roles") or []
                # the whole whoami verdict rides the boot log: the
                # Downloads copy then answers "why is the admin item
                # absent" without adb, and carries the user_hash for
                # admin.txt seeding when the answer is a missed grant
                print("whoami ok: admin=%s mod=%s roles=[%s] "
                      "user_hash=%s" % (
                          self.i_am_admin, self.i_am_moderator,
                          ",".join(r.get("name", "")
                                   for r in self.my_roles),
                          res.get("user_hash", "")), flush=True)
                # task 6: the guild icon rides whoami; ingest it per server
                guild = res.get("guild") or {}
                idx = self._active_server_idx
                srv = None
                if 0 <= idx < len(self._servers):
                    srv = self._servers[idx]
                icon_blob = (guild.get("icon") or "").strip()
                if icon_blob and srv is not None:
                    if srv.get("icon_blob") != icon_blob:
                        Clock.schedule_once(
                            lambda dt: self._ingest_guild_icon(
                                icon_blob, srv), 0)
                if srv is not None:
                    self._repair_recorded_assets(srv)
                banner_blob = (guild.get("banner") or "").strip()
                if banner_blob and srv is not None:
                    if srv.get("banner_blob") != banner_blob:
                        Clock.schedule_once(
                            lambda dt: self._ingest_banner(
                                banner_blob, srv), 0)
                # P6: the message-of-the-day strip under the banner
                Clock.schedule_once(
                    lambda dt: self._apply_motd(
                        (guild.get("motd") or "").strip()), 0)
                # P11 arrow 5: the server's version + toggle -> the strip
                Clock.schedule_once(
                    lambda dt: self._apply_update_hint(
                        (guild.get("app_version") or ""),
                        (guild.get("update_channel") or "")), 0)
        threading.Thread(target=work, daemon=True).start()

    def _apply_update_hint(self, server_version, channel):
        """Pure decision through client_update; "" clears the strip."""
        import client_update
        offer = client_update.update_decision(
            self._client_ver(), server_version, channel)
        self.client_update_version = (
            ".".join(str(x) for x in offer) if offer else "")

    def _client_ver(self):
        return _client_version()

    def _apply_motd(self, value):
        """Bind whoami's guild motd to the sidebar strip ('' hides it)."""
        self.server_motd = value or ""

    # ------------------------------------------------- P11 arrow 5 flow
    def _open_update_prompt(self):
        target = self.client_update_version or ""
        if not target:
            return
        box = BoxLayout(orientation="vertical", padding=dp(10),
                        spacing=dp(8))
        ask = Label(markup=True, font_size=sp(14),
                    color=self.theme_colors.get(
                        "text", (0.92, 0.94, 0.96, 1)))
        ask.text = ("Update to %s?\nUpgrading over the install keeps your "
                    "account; uninstalling first needs your recovery kit "
                    "to hand it to the new install." % target)
        ask.bind(width=lambda inst, w: setattr(inst, "text_size",
                                               (w, None)))
        box.add_widget(ask)
        status = Label(markup=True, font_size=sp(12),
                       color=self.theme_colors.get(
                           "text_dim", (0.7, 0.72, 0.75, 1)))
        status.text = ""
        box.add_widget(status)
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        # tay (2026-11-20): accounts must transfer between clients during
        # the upgrade - the kit gate rides the consent popup, the kit sheet
        # (recovery.open_recovery_sheet) exports identity + username +
        # friends + servers outside the app sandbox BEFORE the install
        # intent is offered
        kit = Button(text="Save kit first")
        go = Button(text="Download and install")
        cancel = Button(text="Not now")
        row.add_widget(kit)
        row.add_widget(go)
        row.add_widget(cancel)
        box.add_widget(row)
        popup = Popup(title="sHades update", content=box,
                      size_hint=(0.86, 0.5), auto_dismiss=True)
        cancel.bind(on_press=lambda inst: popup.dismiss())
        kit.bind(on_press=lambda inst: Clock.schedule_once(
            lambda dt: self.open_recovery_sheet(), 0))
        go.bind(on_press=lambda inst: (
            popup.dismiss(),
            Clock.schedule_once(
                lambda dt: self._update_start(inst), 0)))
        self._update_status_label = status
        import ui_fx
        ui_fx.popup_entrance(popup)

    def _update_start(self, *a):
        """The consented path, on a worker thread; every leg reports
        into update_status (the popup is closed; the status shows on
        the next open)."""
        import client_update

        def note(txt):
            Clock.schedule_once(lambda dt: setattr(self, "update_status",
                                                   txt), 0)

        def work():
            try:
                note("checking the release...")
                manifest = json.loads(
                    client_update.fetch(client_update.LATEST_PATTERN))
                pick = client_update.newest_client_asset(
                    manifest.get("assets") or [], since=_client_version())
                if pick is None:
                    note("no newer arm64 build found on %s"
                         % client_update.REPO)
                    return
                note("fetching the sha for %s..." % pick["apk_name"])
                sha_hex = client_update.parse_sha_file(
                    client_update.fetch(pick["sha_url"]))
                note("downloading %s (DownloadManager)..."
                     % pick["apk_name"])
                blob = client_update.download_with_dm(
                    pick["apk_url"], pick["apk_name"], sha_hex)
                note("sha ok; parking %s..." % pick["apk_name"])
                path = client_update.save_candidate(
                    blob, pick["apk_name"], cache_dir())
                note("handing %s to the installer..." % path)
                client_update.install_intent(path, pick["apk_name"])
                note("installer handed off")
            except client_update.HttpError as e:
                note("update failed: %s" % e)
            except Exception as e:
                note("update failed: %s" % e)

        threading.Thread(target=work, daemon=True).start()

    # tay (channel kinds): "Channels can be text, site (micron pages), or
    # a text stream." Server CHANNEL_KINDS keys are stable ("chat", "site",
    # "term") — existing channels keep working; the labels are the user's
    # words. "term" IS the text-stream channel: text arrives as live
    # pushed rows, no chat framing.

    # -- friends + DMs (U7, client-local) -----------------------------------
    def _toggle_friend(self, user_hash, username):
        """Add or remove a friend; persists friends.json and refreshes the
        sidebar. The guild is not involved."""
        if not user_hash or user_hash == self.my_hash:
            return
        if dms.is_friend(self._friends, user_hash):
            self._friends = dms.remove_friend(self._friends, user_hash)
            self._set_status("removed friend @%s" % (username or user_hash[:8]))
        else:
            self._friends = dms.add_friend(self._friends, user_hash, username)
            self._set_status("added friend @%s" % (username or user_hash[:8]))
        dms.save_friends(self._friends_path, self._friends)
        self._refresh_members_ui(getattr(self.root.get_screen("main"),
                                         "_current_channel", "general")
                                 if self.root else "general")

    def _open_dm_popup(self, peer_hash, nickname):
        """DM thread popup: local history + direct LXMF send. History is
        client-local (dms/<hash>.json); the guild relays nothing."""
        if not peer_hash or peer_hash == self.my_hash:
            return
        box = BoxLayout(orientation="vertical", spacing=4, padding=4)
        title = Label(text="dm: @%s" % (nickname or peer_hash[:8]),
                      size_hint_y=None, height=22)
        box.add_widget(title)
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, size_hint_y=None, spacing=2, padding=[2, 2])
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        box.add_widget(scroll)
        row = BoxLayout(size_hint_y=None, height=40, spacing=4)
        text_in = TextInput(hint_text="message (direct lxmf)",
                            multiline=False,
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        row.add_widget(text_in)
        send = Button(text="send", size_hint_x=None, width=70,
                      size_hint_y=None, height=scaled(40),
                      font_size=sp(14), pos_hint={"center_y": 0.5})
        row.add_widget(send)
        box.add_widget(row)
        close = Button(text="close", size_hint_y=None, height=scaled(40))
        box.add_widget(close)
        popup = Popup(title="dm", content=box, size_hint=(0.7, 0.7))
        self._dm_popup = popup
        self._dm_popup_peer = peer_hash
        self._dm_popup_grid = grid
        self._dm_popup_input = text_in

        def do_send():
            text = (text_in.text or "").strip()
            if not text:
                return
            text_in.text = ""
            self._send_dm(peer_hash, text)
        send.bind(on_press=lambda inst: do_send())
        text_in.bind(on_text_validate=lambda inst: do_send())
        close.bind(on_press=lambda inst: popup.dismiss())
        self._refresh_dm_popup()
        import ui_fx
        ui_fx.popup_entrance(popup)

    def _refresh_dm_popup(self):
        """Re-render the open DM thread from local history."""
        grid = self._dm_popup_grid
        peer = self._dm_popup_peer
        if not grid or not peer:
            return
        grid.clear_widgets()
        try:
            rows = dms.load_dm_history(dms.dm_history_path(_app_data_dir(),
                                                           peer))
        except ValueError:
            rows = []
        for r in rows:
            who = "me" if r["dir"] == "out" else "them"
            state = r.get("state") if r["dir"] == "out" else None
            mark = (" (%s)" % state) if state else ""
            grid.add_widget(Label(
                text="%s: %s%s" % (who, r["text"][:200], mark),
                size_hint_y=None, height=dp(24),
                color=(0.85, 0.87, 0.9, 1) if r["dir"] == "out"
                else (1, 1, 1, 1),
                font_size=sp(12), halign="left", valign="middle"))
            if state == "failed":
                uid = r.get("uid")
                if uid:
                    from kivymd.uix.button import \
                        MDButton, MDButtonText  # noqa: F401
                    btn = MDButton(style="tonal")
                    btn.size_hint_y = None
                    btn.height = dp(24)
                    btn.bind(
                        on_press=lambda inst, u=uid, p=peer:
                        self.retry_dm(p, u))
                    btn.add_widget(MDButtonText(
                        text="Retry", font_size=sp(11),
                        text_color=self.theme_colors["text"]))
                    grid.add_widget(btn)
        if not rows:
            grid.add_widget(Label(text="no messages yet",
                                  size_hint_y=None, height=dp(24),
                                  color=(0.6, 0.63, 0.68, 1),
                                  font_size=sp(12)))

    def _send_dm(self, peer_hash, text):
        """Direct client-to-client LXMF send (U7). The guild is not a party."""
        if not self.lxmf_router or not self.lxmf_delivery:
            self._set_status("dm unavailable (lxmf router offline)")
            return
        text = (text or "").strip()
        if not text:
            return

        def work():
            try:
                import LXMF
                nid = RNS.Identity.recall(bytes.fromhex(peer_hash))
                if nid is None:
                    Clock.schedule_once(lambda dt: self._set_status(
                        "dm: identity unknown for %s — they must be "
                        "online/announced" % peer_hash[:8]), 0)
                    return
                dest = RNS.Destination(nid, RNS.Destination.OUT,
                                       RNS.Destination.SINGLE,
                                       "lxmf", "delivery")
                # LXMF needs a transport PATH to the peer's delivery
                # destination, not just their identity: without one the
                # message is never transmitted. Ask for it and wait briefly.
                if not RNS.Transport.has_path(dest.hash):
                    RNS.Transport.request_path(dest.hash)
                    for _ in range(30):
                        if RNS.Transport.has_path(dest.hash):
                            break
                        time.sleep(0.2)
                    if not RNS.Transport.has_path(dest.hash):
                        Clock.schedule_once(lambda dt: self._set_status(
                            "dm: no path to %s — they must be reachable"
                            % peer_hash[:8]), 0)
                        return
                lxm = LXMF.LXMessage(dest, self.lxmf_delivery, text,
                                     title="sHades dm",
                                     desired_method=LXMF.LXMessage.DIRECT)
                # tier-2 delivery-state lane: the queued row becomes the
                # source of truth for this attempt; the LXMF callbacks
                # (verified in lxmf 0.9.4: LXMRouter.py:2401 calls
                # failed_callback(lxmessage), LXMessage.__mark_delivered
                # calls the registered delivery callback with the lxm)
                # flip it delivered/failed on the router thread via Clock.
                # Registration happens BEFORE handle_outbound: the router
                # spawns process_outbound synchronously, so a late
                # registration can miss the deliver attempt.
                path = dms.dm_history_path(_app_data_dir(), peer_hash)
                row = {}
                try:
                    row = dms.append_dm(path, "out", text, state="queued")
                except ValueError:
                    pass
                if row.get("uid"):
                    uid = row["uid"]

                    def _flip(new_state):
                        def cb(_lxm):
                            try:
                                dms.set_dm_state(path, uid, new_state)
                            except Exception as exc:
                                print("dm state write failed:", repr(exc))
                            Clock.schedule_once(
                                lambda dt: self._refresh_dm_popup(), 0)
                        return cb

                    lxm.register_delivery_callback(_flip("delivered"))
                    lxm.register_failed_callback(_flip("failed"))
                    Clock.schedule_once(
                        lambda dt: self._refresh_dm_popup(), 0)
                self.lxmf_router.handle_outbound(lxm)
            except Exception as e:
                err = repr(e)
                Clock.schedule_once(lambda dt: self._set_status(
                    "dm send failed: %s" % err), 0)
                return
            Clock.schedule_once(lambda dt: self._refresh_dm_popup(), 0)
        threading.Thread(target=work, daemon=True).start()

    def retry_dm(self, peer_hash, uid):
        """tier-2 retry lane: resend a failed DM attempt in place.

        The failed row (uid) is removed and the same text is queued as
        a fresh attempt; the delivery-state callbacks follow the new
        row the same way _send_dm wires them.
        """
        path = dms.dm_history_path(_app_data_dir(), peer_hash)
        text = None
        for row in dms.load_dm_history(path):
            if row.get("uid") == uid:
                text = row.get("text")
                break
        if not text:
            return
        dms.remove_dm_row(path, uid)
        Clock.schedule_once(lambda dt: self._refresh_dm_popup(), 0)
        self._send_dm(peer_hash, text)

    def _on_dm_delivery(self, lxm):
        """LXMF delivery callback: everything arriving at our lxmf.delivery
        destination is a direct DM (guild chat never uses this path)."""
        try:
            src = hexstr(lxm.source_hash)
            content = lxm.content
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            content = (content or "").strip()
            if not content:
                return
            try:
                dms.append_dm(dms.dm_history_path(_app_data_dir(), src),
                              "in", content)
            except ValueError:
                return
            nick = next((f["nickname"] for f in self._friends
                         if f["user_hash"] == src), None) or ("user-" + src[:6])
            if self._dm_popup_peer == src:
                Clock.schedule_once(lambda dt: self._refresh_dm_popup(), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("dm from @%s: %s"
                                                % (nick, content[:60])), 0)
        except Exception as e:
            print("dm delivery error:", repr(e))

    # -- admin panel (U8) --------------------------------------------------

    def _check_join_gate(self):
        """Ask the guild whether this client may register. Returns True if
        registration is allowed right now; otherwise prompts for a code."""
        if not self.guild_api:
            return True

        def work():
            try:
                res = self.guild_api.join()
            except Exception:
                return
            if not res.get("ok"):
                return
            if not res.get("required") or res.get("joined"):
                return
            Clock.schedule_once(lambda dt: self._open_join_prompt(), 0)
        threading.Thread(target=work, daemon=True).start()

    # -- context menus (U3) ------------------------------------------------
    def open_context_menu(self, actions, window_pos):
        """Show the Window-level context menu overlay."""
        if not actions:
            return
        if self._ctx_menu is not None:
            self._ctx_menu.dismiss_now()
        self._ctx_menu = ContextMenuOverlay(actions, window_pos)
        Window.add_widget(self._ctx_menu)

    def _copy_bootlog(self):
        """paste-able boot-log handoff: no adb, no MediaStore. The
        clipboard bridge (kivy) is proven on-device; MediaStore was
        not, and tay's device miss proved it wrong in exactly the
        way a second bridge can bypass. Long-press -> tap -> paste."""
        try:
            from main import _log_path
            with open(_log_path(), "r", encoding="utf-8",
                      errors="replace") as f:
                text = f.read()
            tail = text[-20000:]
            if not tail.strip():
                self._set_status("boot log empty")
                return
            self._copy_text(tail)
            print("boot log handed to clipboard: %d chars"
                  % len(tail), flush=True)
        except Exception as e:
            print("boot log clipboard failed:", repr(e), flush=True)
            self._set_status("boot log copy failed")

    def _reply_quote(self, username, content):
        """Stage a reply card. The wire prefix stays readable on older clients."""
        _author, _excerpt, body = split_reply(content)
        self._pending_reply = (username, body)
        self.reply_draft = "Replying to @%s · %s" % (
            username, " ".join(body.split())[:80])

        try:
            main = self.root.get_screen("main")
            inp = main.ids.msg_input
            inp.focus = True
        except Exception as e:
            print("reply stage failed:", repr(e))

    def cancel_reply(self):
        self._pending_reply = None
        self.reply_draft = ""

    def _mention(self, username):
        try:
            main = self.root.get_screen("main")
            inp = main.ids.msg_input
            inp.text = (inp.text or "") + "@%s " % username
            inp.focus = True
        except Exception as e:
            print("mention failed:", repr(e))

    # -- slash commands (U4) -----------------------------------------------

    def send_raw_text(self, text):
        """Post text to the current channel bypassing slash handling."""
        try:
            main = self.root.get_screen("main")
            name = getattr(main, "_current_channel", "general")
            cid = self._channel_cid(name)
        except Exception as e:
            print("raw send failed:", repr(e))
            return
        if not text.strip() or not self.guild_api:
            return

        def work():
            res = self.guild_api.post(cid, text)
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("send failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _switch_channel(self, name):
        main = self.root.get_screen("main")
        main._current_channel = name
        if getattr(main, "width", dp(720)) < dp(720):
            main.left_open = False
        channel_grid = getattr(main.ids, "channel_grid", None)
        if channel_grid is not None:
            for button in channel_grid.children:
                if isinstance(button, ChannelButton):
                    button.selected = button.channel_name == name
        # Keep the channel header in sync with the active channel.
        try:
            main.ids.channel_header.text = "#" + name
        except Exception:
            pass
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._remove_skeleton(getattr(self, "_skeleton_cid", None))
        self._msg_widgets.clear()
        self._seq = 0
        self._last_ts = 0.0
        cid = self._channel_cid(name)
        kind = self._channel_kind(cid)
        # sitenav (d): leaving a site channel hides the nav bar; the
        # site opener shows it again.
        if kind != "site":
            _refresh_nav = getattr(self, "_refresh_site_nav", None)
            if _refresh_nav is not None:
                _refresh_nav(visible=False)
        # P8: the composer's truth follows the channel everywhere
        self._sync_composer_props(name, cid)
        # reroute the stream: only the visible channel flows
        self._stream_cid = cid
        self._stream_cursor = None
        if kind not in ("chat", "announce"):
            # site/term channels carry no live push: release the stream
            # slot (one subscription per link) so the previous channel
            # stops flowing into a channel nobody is watching
            self._stream_cid = None
            threading.Thread(target=self._release_stream,
                             daemon=True).start()
            if kind == "site":
                self._open_site_channel(name, cid)
                return
            self._open_term_channel(name, cid)
            return
        self._older.setdefault(cid, {"oldest_id": None, "has_older": True,
                                     "loading": False})
        cached = self._show_cached_channel(grid, cid)
        if self.guild_api:
            if not cached:
                self._show_skeleton(cid)
            self._fetch_messages(name)
        elif not cached:
            self._set_status("No cached messages for #%s yet." % name)
            # H7b: offline + empty cache is still an empty channel.
            # getattr: minimal test stubs bind _switch_channel without
            # the rest of the app.
            _refresh_ph = getattr(self, "_refresh_empty_placeholder", None)
            if _refresh_ph is not None:
                _refresh_ph(grid)
        self._refresh_members_ui(name)
        # v0.31: re-subscribe NOW. The server binds the channel at
        # subscribe time; setting _stream_cid alone changed nothing on
        # the wire, and live updates died after the first switch.
        Clock.schedule_once(lambda dt: self._start_streamer(), 0)

    # -- site channels (micron pages over RNS; no HTML anywhere) ---------

    # -- site editor (P7c): edit the page on screen, then publish -------
    SITE_STARTER_INDEX_MU = (
        "#!c=0\n"
        "> A fresh sHades site\n"
        "-\n"
        "This channel is a micron site. Pages live as site resources\n"
        "served from this guild; the default template at /index.mu\n"
        "stands in until you publish your own pages.\n"
        "-\n"
        "Quick micron form:\n"
        "`!bold!` `*italic*` `_underline_` `F8a8`code`f`\n"
        "[publish with site/put `/index.mu]\n"
    )

    def _site_editor_publish(self):
        """Store the edited source for the page the editor opened on."""
        try:
            scr = self.root.get_screen("site_editor")
        except Exception:
            return
        cid = getattr(self, "_edit_cid", None)
        path = getattr(self, "_edit_path", None)
        if not cid or not path:
            scr._status.text = "no page target"
            return
        src = scr._src.text
        scr._status.text = "publishing…"

        def work():
            try:
                res = self.guild_api.site_put(
                    cid, path,
                    base64.b64encode(src.encode("utf-8")).decode("ascii"))
            except Exception as e:
                res = {"ok": 0, "error": repr(e)}
            ok = res.get("ok") == 1
            err = "%s (code %s)" % (res.get("error", ""),
                                    res.get("code", "?"))
            Clock.schedule_once(
                lambda dt: self._site_editor_publish_done(ok, err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _member_row(self, user_hash, uname, presence_color):
        """Build a sidebar row with the same avatar resolution as the
        message rows. tay (2026-11-19): the userlist showed a white
        square because rows were built with avatar_source="" -- the
        sidebar never resolved from IMAGE_CACHE and never fetched.
        Registered in _member_widgets so _refresh_avatar rebinds these
        rows too when a fetch lands.
        """
        avatar_source = ""
        if user_hash == self.my_hash and getattr(
                self, "my_avatar_small", None):
            avatar_source = self.my_avatar_small
        else:
            key = "av:" + (user_hash or "")
            path = IMAGE_CACHE.get(key)
            if path:
                avatar_source = path
            elif user_hash and IMAGE_CACHE.begin_load(key):
                def _fetch(a=user_hash, k=key):
                    try:
                        res = self.guild_api.avatar_get(a)
                        if res.get("ok") and res.get("avatar"):
                            IMAGE_CACHE.set_bytes(k, res["avatar"], "png")
                            Clock.schedule_once(
                                lambda dt: self._refresh_avatar(k), 0)
                    finally:
                        IMAGE_CACHE.end_load(k)
                threading.Thread(target=_fetch, daemon=True).start()
        row = MemberRow(
            username=uname,
            avatar_source=avatar_source,
            author_hash=user_hash,
            presence_color=presence_color,
        )
        self._member_widgets[user_hash] = row
        return row

    def _refresh_members_ui(self, channel):
        # Widget access must happen on the main thread.
        Clock.schedule_once(lambda dt: self._refresh_members_ui_main(channel), 0)

    def _refresh_members_ui_main(self, channel):
        """Render the member sidebar.

        Online members come from the guild's active-user list (the ping
        registry), so the sidebar shows who is present *now*: a member whose
        client stops pinging is dropped by the guild's reaper and disappears
        from here on the next presence tick. The list therefore prunes itself
        and never accumulates users who have gone away.

        If the guild exposes no presence endpoint, fall back to the authors seen
        this session, shown as unknown/offline rather than hidden.
        """
        try:
            main = self.root.get_screen("main")
            grid = main.ids.member_grid
            grid.clear_widgets()
            self._member_widgets = {}
            # Friends first (U7): local roster, online state best-effort from
            # the guild presence map (a friend away from the guild shows gray).
            if self._friends:
                grid.add_widget(Label(
                    text="friends (%d)" % len(self._friends),
                    size_hint_y=None, height=dp(22), color=(1, 0.85, 0.5, 1),
                    font_size=sp(12), bold=True))
                for f in self._friends:
                    online = f["user_hash"] in self._active_users
                    grid.add_widget(self._member_row(
                        f["user_hash"],
                        f.get("nickname") or ("user-" + f["user_hash"][:6]),
                        [0.2, 0.8, 0.2, 1] if online else [0.5, 0.5, 0.5, 1]))
                grid.add_widget(Label(
                    text="members", size_hint_y=None, height=dp(22),
                    color=(0.7, 0.73, 0.77, 1), font_size=sp(12), bold=True))
            if self._presence_ok:
                for user_hash, info in sorted(
                        self._active_users.items(),
                        key=lambda kv: (kv[1].get("username") or "").lower()):
                    grid.add_widget(self._member_row(
                        user_hash,
                        info.get("username") or ("user-" + user_hash[:6]),
                        [0.2, 0.8, 0.2, 1]))   # green = online
                if not self._active_users:
                    # A Label, not a MemberRow: rows are clickable profiles.
                    grid.add_widget(Label(
                        text="nobody online", size_hint_y=None, height=dp(28),
                        color=(0.7, 0.73, 0.77, 1), font_size=sp(12)))
                return
            # Fallback: authors seen this session; presence unknown.
            for h, uname in list(self._members.items())[:50]:
                grid.add_widget(self._member_row(
                    h, uname, [0.5, 0.5, 0.5, 1]))
        except Exception as e:
            print("members ui error:", e)

    def _record_member(self, author_hash, username):
        if author_hash and username:
            self._members[author_hash] = username

    # -- input -----------------------------------------------------------
    def send_message(self):
        main = self.root.get_screen("main")
        inp = main.ids.msg_input
        text = (inp.text or "").strip()
        if not self.guild_api:
            return
        cid = getattr(main, "_current_channel", "general")
        # term channels: the input bar drives the guild console instead
        if self._channel_kind(self._channel_cid(cid)) == "term":
            if text:
                inp.text = ""
                self._send_term_input(text)
            return
        # slash commands: client-local first, then the shared dispatcher
        if text.startswith("/"):
            inp.text = ""
            self._handle_slash(text, cid)
            return
        # Allow attachment-only messages (no text) when a file is pending.
        if not text and not getattr(self, "_pending_attachment", None):
            return
        # fold-14 P11g: capture the reply BEFORE reply_wire consumes the
        # card, and keep the RAW text: the restore hands back what the
        # member typed, and the re-staged card re-applies the prefix on
        # the retry (no double prefix ever lands on the wire).
        old_reply = self._pending_reply
        raw_text = text
        if self._pending_reply:
            text = reply_wire(*self._pending_reply, text)
            self.cancel_reply()
        cid = self._channel_cid(cid)
        inp.text = ""
        # Capture ONCE here (main thread): a second send during this
        # worker's flight must not mutate what this one posts.
        att = getattr(self, "_pending_attachment", None)

        def _send_failed(reason):
            """No send failure costs the member their words (P11g):

            a dead link raised out of the old worker with no except while
            the composer had already cleared - text lost, no status, on
            every failure leg. Now: the raw pre-reply_wire text returns
            to the composer, the consumed reply card re-stages, the
            attachment stays pending, and the status line says so. The
            field write rides the Clock (worker-thread law)."""
            def restore(dt=None):
                try:
                    inp.text = raw_text
                except Exception:
                    pass
                if old_reply:
                    self._pending_reply = old_reply
                    self.reply_draft = "Replying to @%s · %s" % (
                        old_reply[0], " ".join(old_reply[1].split())[:80])
                self._set_status(
                    "send failed: %s (text restored)" % str(reason)[:80])
            Clock.schedule_once(restore, 0)

        def work():
            try:
                _work_inner()
            except Exception as exc:
                # the last-ditch net: a raise here is a dead link or an
                # unexpected error - lose nothing, restore via the Clock
                self._up_active = False
                _send_failed(str(exc)[:120])

        def _work_inner():
            atts_req = None
            atts_render = None
            if att:
                name, mime, blob = att
                # v0.30: the blob NEVER rides the message packet. Inline
                # triples exceed the link packet budget (receipt: tay,
                # "packet size of 515 exceeds mtu of 500") -- the chunked
                # blob API moves every byte; the post carries a reference.
                # v0.32: progress is visible in the status line, so a slow
                # mobile uplink reads as movement, not silence.
                Clock.schedule_once(
                    lambda dt: self._set_status("attach upload starting"),
                    0)

                def _up_progress(done, total):
                    Clock.schedule_once(
                        lambda dt: self._set_status(
                            "attach upload %d%%"
                            % int(100 * done / max(total, 1))), 0)

                self._up_active = True
                try:
                    up = self.guild_api.blobs_put(name, mime, blob,
                                                  on_progress=_up_progress)
                finally:
                    self._up_active = False
                if not up.get("ok"):
                    _send_failed("attach upload (%s): %s"
                                 % (up.get("phase", "init"),
                                    up.get("error", "unknown")[:80]))
                    return
                atts_req = [{"blob_id": up.get("blob_id"),
                             "name": name, "mime": mime}]
            if atts_req:
                res = self.guild_api.post_v1(cid, text, attachments=atts_req)
                # the server answers with stored metadata (name, mime,
                # size, path) and no bytes -- the optimistic render is the
                # same shape push delivers to everyone else, so it renders
                # lazily through _fetch_attachment just like received rows
                atts_render = res.get("attachments") or None
            else:
                res = self.guild_api.post(cid, text)
            if res.get("ok"):
                # optimistically show
                msg_id = res.get("msg_id")
                m = {"msg_id": msg_id, "author_hash": self.my_hash,
                     "username": self.username, "content": text,
                     "ts": now(), "channel_id": cid,
                     "attachments": atts_render}
                Clock.schedule_once(lambda dt: self._render_message(
                    main.ids.message_grid, m, animate=True), 0)
                self._pending_attachment = None
            else:
                err = res.get("error", "unknown")
                _send_failed(err)
        threading.Thread(target=work, daemon=True).start()

    def _on_key_down(self, window, key, scancode, codepoint, modifier):
        """Keyboard hook for the message input and connect screen.

        Plain Enter sends the message; Shift+Enter inserts a newline
        (the multiline input handles the newline itself). Tab moves focus
        to the next input field on the connect screen. Returns True to
        claim the key when we handled it.
        """
        # Ctrl+K opens the tier-2 command palette (desktop)
        if key == 107 and isinstance(modifier, (list, tuple)) and any(
                str(m).lower() in ("ctrl", "lctrl", "rctrl")
                for m in modifier):
            self.open_command_palette()
            return True
        # Tab focus traversal on the connect screen
        if key == 9:
            try:
                connect = self.root.get_screen("connect") if self.root else None
                if connect and hasattr(connect.ids, "hub_address"):
                    fields = [
                        connect.ids.hub_address,
                        connect.ids.username,
                        connect.ids.node_hash,
                        connect.ids.delivery_hash,
                    ]
                    focused = next(
                        (f for f in fields if getattr(f, "focus", False)), None
                    )
                    if focused is not None:
                        idx = fields.index(focused)
                        next_idx = (idx + 1) % len(fields)
                        fields[next_idx].focus = True
                        return True
            except Exception:
                pass

        main = self.root.get_screen("main") if self.root else None
        inp = main.ids.msg_input if main else None
        if inp and getattr(inp, "focus", False) and key in (13, 327):
            if "shift" not in (modifier or []):
                self.send_message()
                return True
        return None

    # -- attachments: server-stored vs ephemeral (P2P) -------------------

    # ---------------- Android SAF file picking ---------------------------
    # kivy's FileChooserListView browses the app sandbox only, which on
    # Android contains no user files, so chooser popups showed nothing
    # (tay's receipt). The system picker (ACTION_OPEN_DOCUMENT) needs no
    # storage permission and returns a content:// uri read through the
    # app's ContentResolver into a local copy, then the normal pipeline
    # takes over (_send_attached_file / _on_file_selected).

    def _send_with_attachment(self, text, name, mime, blob, style):
        """Post a message with one attachment and optimistically render it.

        The wire format is always a [name, mime, blob] triple; the chosen
        style only affects how the client persists/re-fetches the attachment.
        """
        print(
            f"[TRACE] _send_with_attachment: name={name} mime={mime} "
            f"size={len(blob)} style={style} text_len={len(text or '')}",
            flush=True)
        dlog(
            f"_send_with_attachment: name={name} mime={mime} "
            f"size={len(blob)} style={style} text_len={len(text or '')}")
        if not self.guild_api:
            self._set_status("Not connected to guild")
            return
        main = self.root.get_screen("main")
        cid = getattr(main, "_current_channel", "general")
        cid = self._channel_cid(cid)

        def work():
            try:
                print(f"[TRACE] _send_with_attachment.work: posting to channel={cid}", flush=True)
                dlog(f"_send_with_attachment.work: posting to channel={cid}")

                def stage(msg):
                    Clock.schedule_once(
                        lambda dt, m=msg: self._set_status(m), 0)

                def bstage(msg):
                    # 0.31.10: stage onto the composer helper text -- the
                    # only progress surface visible from inside a channel.
                    Clock.schedule_once(
                        lambda dt, m=msg: self._attach_composer_stage(m), 0)

                # polish (g): the bar setter lives on AttachmentsMixin.
                # Stub apps that bind _send_with_attachment a la carte
                # (e.g. the group-2 entrance test's SimpleNamespace)
                # may not carry it -- degrade to text-only progress
                # instead of raising mid-send.
                set_progress = getattr(self, "_set_upload_progress", None)

                def pstage(done, total):
                    pct = int(100 * done / max(total, 1))
                    # polish (g): the bar is the visual surface for the
                    # same pct the helper text reports. pstage runs on
                    # the worker thread, so hop onto the main thread
                    # like bstage does; the bar jumps to the reported
                    # pct (state, not motion).
                    if set_progress is not None:
                        Clock.schedule_once(
                            lambda dt, p=pct: set_progress(p), 0)
                    bstage("attachment: uploading %s (%d%%)" % (name, pct))

                # v1 path: upload the blob once, post by reference (no re-transfer
                # on retry). Fall back to the legacy inline msgpack post when the
                # guild predates /api/v1.
                # 0.31.8 (boot.log receipt): the SAF picker stop aborts the TCP
                # interface ([Errno 103]) for ~5 s; RNS silently reconnects, but
                # a chunk request already in flight is lost and blobs_put
                # reports "timeout". One shot meant every attach died there,
                # then fell through to the legacy inline post, which also
                # failed. Chunk uploads are idempotent (offset-addressed), so
                # retry the whole flow around the socket gap.
                res = None
                up = None
                # 0.31.10 comment correction (boot_2 13:23:20 + boot_3
                # 13:47:58 receipts): the "guild link dead (3)" during the
                # window is the RNS link object reporting CLOSED -- the
                # server's aftereffect of the SAF abort, ~15-25 s later --
                # NOT our keepalive reaper (no "keepalive miss" lines fire
                # while the gate is held). The gate's real job: keep OUR
                # reaper from stacking a second tear, and hold the retry
                # alive through the sleep so it lands on the re-established
                # link (boot_3: the mp3 posted on attempt 2).
                self._up_active = True
                try:
                    for attempt in range(4):
                        # polish (g): the bar shows from 0% at the
                        # start of every attempt (a retry re-uploads
                        # from scratch, so a stale pct would lie).
                        if set_progress is not None:
                            Clock.schedule_once(
                                lambda dt: set_progress(0), 0)
                        bstage("attachment: uploading %s (%d KB)%s"
                               % (name, len(blob) // 1024,
                                  "" if attempt == 0 else " (attempt %d)" % (attempt + 1)))
                        try:
                            up = self.guild_api.blobs_put(name, mime, blob,
                                                          on_progress=pstage)
                        except Exception as e:
                            dlog("v1 blob path failed: %r" % (e,))
                            print("[TRACE] v1 blob path failed, falling back:",
                                  repr(e), flush=True)
                            break
                        if up.get("ok"):
                            break
                        err = up.get("error", "unknown")
                        print("[TRACE] blob upload attempt %d failed: %s"
                              % (attempt + 1, err), flush=True)
                        link = getattr(getattr(self, "guild_api", None),
                                       "link", None)
                        dead = link is None or getattr(
                            link, "status", None) not in (None, RNS.Link.ACTIVE)
                        if attempt < 3 and err == "timeout" and not dead:
                            # Only the receipted socket-gap timeout retries; a
                            # dead link is the re-establish path's job.
                            bstage("attachment: waiting for the link (%s, "
                                   "attempt %d) -- will not give up for "
                                   "%d s" % (err, attempt + 1,
                                             (3 - attempt) * 8))
                            time.sleep(8.0)
                            continue
                        bstage("attachment: upload failed (%s); link lost "
                               "-- retry the attach" % err)
                        break
                finally:
                    self._up_active = False
                if up is not None and up.get("ok"):
                    bstage("attachment: blob ok, posting %s" % name)
                    try:
                        res = self.guild_api.post_v1(
                            cid, text,
                            attachments=[{"name": name, "mime": mime,
                                          "blob_id": up["blob_id"]}])
                        dlog("send.post_v1 response: %r" % (res,))
                    except Exception as e:
                        # The blob itself is already uploaded; a raise here is
                        # the link/receipt layer, not the upload. Do NOT fall
                        # through to the legacy inline post (it would push the
                        # full blob again over a flapping link -- the
                        # receipted failure mode).
                        dlog("post_v1 raised after blob ok: %r" % (e,))
                        print("[TRACE] post_v1 raised after blob ok:", repr(e), flush=True)
                        bstage("attachment: %s uploaded, post failed "
                               "-- try again; blob may be reused" % name)
                        return
                if res is None or not res.get("ok"):
                    # 0.31.9: the legacy inline post is the pre-/api/v1
                    # fallback only. ANY blobs_put failure after the retry
                    # loop (timeout or link dead) must never fall through:
                    # boot_2 receipt at 13:23:22 -- a "link dead" race
                    # between attempts bypassed the timeout-only guard and
                    # the inline post pushed the full blob again.
                    if res is None and up is not None and not up.get("ok"):
                        return
                    try:
                        stage("attachment: posting inline %s ..." % name)
                        legacy = self.guild_api.post(
                            cid, text, attachments=[[name, mime, blob]])
                        res = legacy if (res is None
                                         or not res.get("ok")) else res
                        dlog("send.legacy post response: %r" % (res,))
                    except Exception as e:
                        # pre-v0.16 bug: a raise here killed the worker
                        # thread silently, leaving a spinner forever.
                        dlog("legacy attachment post raised: %r" % (e,))
                        bstage("attach send failed: %s: %s"
                               % (type(e).__name__, e))
                        return
                if res is None:
                    bstage("attach send failed: no response")
                    return
                print(
                    f"[TRACE] _send_with_attachment.work: post response "
                    f"ok={res.get('ok')} msg_id={res.get('msg_id')} "
                    f"attachment={res.get('attachment')}", flush=True)
                dlog(
                    f"_send_with_attachment.work: post response "
                    f"ok={res.get('ok')} msg_id={res.get('msg_id')} "
                    f"attachment={res.get('attachment')}")
                if not res.get("ok"):
                    bstage("send failed: %s" % res.get("error", "unknown"))
                    return
            except Exception as e:
                import traceback
                dlog("attachment send thread crashed: trace=%s"
                     % traceback.format_exc())
                bstage("attachment send crashed: %s" % type(e).__name__)
                return
            msg_id = res.get("msg_id")
            # The guild may ack with stored metadata (path) => server-stored.
            srv_att = res.get("attachment") or {}
            atts = res.get("attachments") or []
            srv_path = srv_att.get("path") or (atts[0].get("path") if atts else None)
            if style == ATT_SERVER and srv_path:
                dlog(f"_send_with_attachment.work: server-stored path={srv_path}")
                att = {"name": name, "mime": mime, "size": len(blob),
                       "kind": ATT_SERVER, "path": srv_path}
            else:
                # Ephemeral (or server did not ack a path): keep blob locally.
                dlog(f"_send_with_attachment.work: ephemeral style={style} srv_path={srv_path}")
                local_path = self._save_attachment_blob(blob, name)
                att = {"name": name, "mime": mime, "kind": ATT_EPHEMERAL,
                       "local_path": local_path}
            m = {"msg_id": msg_id, "author_hash": self.my_hash,
                 "username": self.username, "content": text, "ts": now(),
                 "channel_id": cid, "attachments": [att]}
            Clock.schedule_once(lambda dt: self._render_message(
                main.ids.message_grid, m, animate=True), 0)
            Clock.schedule_once(
                lambda dt: setattr(main.ids.msg_input, "text", ""), 0)
            # 0.31.10: the attach flow is done -- clear the progress channel.
            Clock.schedule_once(lambda dt: self._attach_composer_stage(""), 0)
            self._add_to_history(m)
            self._queue_cache(m)
            self._save_history()

        def _work_guarded():
            # polish (g): every exit path of work() must clear the
            # progress channel -- v1 success, the legacy inline
            # fallback, and every early `return` on failure (post_v1
            # raise after blob ok, blobs_put failure, legacy raise,
            # no response, worker crash). _attach_composer_stage("")
            # hides the upload bar as well as the helper text, so run
            # it in a finally here; the success path's own clear
            # inside work() is the same idempotent call, scheduled
            # first. work() itself is untouched.
            try:
                work()
            finally:
                Clock.schedule_once(
                    lambda dt: self._attach_composer_stage(""), 0)

        threading.Thread(target=_work_guarded, daemon=True).start()

    # -- profile ----------------------------------------------------------
    def open_profile(self, target_hash, username):
        """Open the profile screen for a user (self or another member).
        Connects to the remote client's node, fetches their profile page
        via RNS link, and renders it in a popup."""
        if not self.guild_api:
            return

        # If no target_hash, show own profile locally
        if not target_hash or target_hash == self.my_hash:
            self._show_own_profile()
            return

        # target_hash is a *user* hash (the message-author key), but the guild's
        # node index is keyed by *node* hash. Presence carries the user->node
        # link, so resolve through it; fall back to the raw hash for old guilds.
        info = self._active_users.get((target_hash or "").lower())
        lookup_hash = (info or {}).get("node_hash") or target_hash

        # Everything below blocks on RNS: the guild lookup, the link setup
        # (up to 20s waiting for ACTIVE), and the page fetch. Run it on a
        # worker thread and hop back to the main thread for the status text
        # and the popup, so opening a profile never freezes the UI.
        def work():
            # v0.29: the directory confirms the node is registered and gives
            # the user it belongs to. Reachability is cryptographic: the
            # node's destination hash is the address (Identity.recall +
            # RNS.Link below); no ip:port involved.
            try:
                res = self.guild_api.lookup_node(lookup_hash)
                if not res.get("ok"):
                    Clock.schedule_once(lambda dt: self._set_status(
                        "Node not found: %s" % res.get("error", "unknown")), 0)
                    return
            except Exception as e:
                err = "Lookup failed: %s" % e
                Clock.schedule_once(
                    lambda dt: self._set_status(err), 0)
                return

            # Connect to remote node via RNS.Link and request profile page
            try:
                nh = bytes.fromhex(lookup_hash)
                nid = RNS.Identity.recall(nh)
                if nid is None:
                    Clock.schedule_once(lambda dt: self._set_status(
                        "RNS does not know identity for %s" % target_hash), 0)
                    return
                nd = RNS.Destination(nid, RNS.Destination.OUT,
                                     RNS.Destination.SINGLE, "nomadnetwork", "node")
                link = RNS.Link(nd)
                for _ in range(200):
                    if link.status == RNS.Link.ACTIVE:
                        break
                    time.sleep(0.1)
                if link.status != RNS.Link.ACTIVE:
                    Clock.schedule_once(lambda dt: self._set_status(
                        "Link to %s never active" % target_hash[:12]), 0)
                    return

                # Request profile page
                req_path = "/page/profile/%s" % target_hash
                receipt = link.request(req_path, data={})
                if isinstance(receipt, bool):
                    if not receipt:
                        Clock.schedule_once(
                            lambda dt: self._set_status("Profile request failed"), 0)
                        return
                    receipt = link.request(req_path, data={})
                deadline = time.time() + 10
                while (hasattr(receipt, "response")
                       and receipt.response is None
                       and time.time() < deadline):
                    time.sleep(0.1)
                if not hasattr(receipt, "response") or receipt.response is None:
                    Clock.schedule_once(
                        lambda dt: self._set_status("Profile request timed out"), 0)
                    return

                profile_text = receipt.response.decode("utf-8", errors="replace")

                # Fetch any images referenced in the profile page
                # (![alt](/file/name))
                image_paths = {}  # filename -> local temp path
                import re as _re
                for m in _re.finditer(r"!\[([^\]]*)\]\(/file/([^)]+)\)", profile_text):
                    filename = m.group(2)
                    try:
                        file_req_path = "/file/%s" % filename
                        file_receipt = link.request(file_req_path, data={})
                        if isinstance(file_receipt, bool):
                            if not file_receipt:
                                continue
                            file_receipt = link.request(file_req_path, data={})
                        deadline = time.time() + 10
                        while (hasattr(file_receipt, "response")
                               and file_receipt.response is None
                               and time.time() < deadline):
                            time.sleep(0.1)
                        if (hasattr(file_receipt, "response")
                                and file_receipt.response is not None):
                            # Save to temp file
                            import tempfile as _tempfile
                            tmp = _tempfile.NamedTemporaryFile(
                                suffix=".png", delete=False, dir=_app_data_dir())
                            tmp.write(file_receipt.response)
                            tmp.close()
                            image_paths[filename] = tmp.name
                    except Exception:
                        pass

                Clock.schedule_once(lambda dt: self._show_profile_popup(
                    target_hash, username, profile_text, image_paths), 0)

            except Exception as e:
                print("Profile fetch error:", repr(e))
                err = "Profile fetch failed: %s" % e
                Clock.schedule_once(
                    lambda dt: self._set_status(err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _contact_card_payload(self):
        """v0.34 contact card payload (profile_card v1 spec).

        The pubkey comes from the client identity, so a card never
        leaks private key data; the alias rides the display name the
        user already owns. Returns None on a fresh run where the router
        (and with it the identity) has not been built yet.
        """
        ident = getattr(self, "client_identity", None)
        if ident is None:
            return None
        alias = (self.display_name
                 or getattr(self, "username", "") or "").strip()
        if not alias:
            return None
        try:
            return profile_card.build_payload(
                ident.get_public_key(), alias)
        except profile_card.CardPayloadError as e:
            print("contact card payload error:", e)
            return None

    def render_contact_card(self):
        """Render the card QR (avatar composited mid-code) into the
        image cache; returns the cached file path or None."""
        payload = self._contact_card_payload()
        if payload is None:
            return None
        try:
            avatar = None
            av_path = getattr(self, "my_avatar_small", "") or ""
            from os.path import isfile as _isfile
            if av_path and _isfile(av_path):
                with open(av_path, "rb") as f:
                    avatar = f.read()
        except Exception:
            avatar = None
        try:
            qr = profile_card.render_qr_png(payload)
            comp = profile_card.composite_center(qr, avatar, out_px=512)
        except profile_card.CardPayloadError as e:
            print("contact card render error:", e)
            return None
        return IMAGE_CACHE.set_bytes("card:qr", comp)

    def share_contact_card(self):
        """Copy the card payload as text (v1 import path: the scanner
        pastes it into their card import field)."""
        payload = self._contact_card_payload()
        if payload is None:
            self._set_status("Card unavailable until the connection starts")
            return
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(payload)
            self._set_status("Card payload copied — send it to a friend")
        except Exception as e:
            self._set_status("Could not copy card: %s" % repr(e))

    def import_contact_card(self):
        """Card slice 3 (tay-approved): paste a shades-contact1 payload
        from the clipboard - parse it, derive the canonical DM address
        offline, and save the member as a friend. No hub needed; the
        payload carries no address to dial, only a keyset to derive
        from (profile_card.derive_user_hash == lxmf.delivery hash)."""
        try:
            from kivy.core.clipboard import Clipboard
            text = Clipboard.paste() or ""
        except Exception as e:
            self._set_status("Clipboard unavailable: %s" % repr(e))
            return
        try:
            pub, alias = profile_card.parse_payload(text)
        except profile_card.CardPayloadError as e:
            self._set_status(
                "Clipboard is not a contact card: %s" % e)
            return
        uh = profile_card.derive_user_hash(pub)
        if uh == self.my_hash:
            self._set_status("That's your own card.")
            return
        self._friends = dms.add_friend(self._friends, uh, alias)
        dms.save_friends(self._friends_path, self._friends)
        self._set_status("Card imported: %s is in your friends list." % alias)

    def _show_own_profile(self):
        """Show own profile locally."""
        try:
            self.profile_screen = self.root.get_screen("profile")
        except Exception:
            self._set_status("Profile screen not available")
            return
        self.profile_screen.target_hash = self.my_hash or ""
        self.profile_screen.target_username = self.username or "user"
        delivery = getattr(self, "lxmf_delivery", None)
        self.profile_screen.target_delivery = (
            hexstr(delivery.hash) if delivery is not None else "")
        # Show saved circular pfp if we have one, otherwise blank
        self.profile_screen.target_avatar = getattr(
            self, "my_avatar_small", ""
        ) or ""
        # v0.34 contact card: render the QR into the cache and bind it
        qr_widget = self.profile_screen.ids.get("profile_card_qr")
        alias_label = self.profile_screen.ids.get("profile_card_alias")
        qr_path = self.render_contact_card()
        if qr_widget is not None:
            if qr_path is not None:
                qr_widget.source = qr_path
                if alias_label is not None:
                    alias_label.text = (
                        self.display_name or self.username or "user")
            else:
                qr_widget.source = ""
                if alias_label is not None:
                    alias_label.text = ""
        try:
            self.root.current = "profile"
        except Exception:
            self._set_status("Could not switch to profile screen")

    def page_profile_handler(self, path, data, request_id, link_id,
                             remote_identity, requested_at):
        """Serve profile page for this client's node.
        Called by remote clients via RNS.Link.request().
        Returns micron/markdown profile page."""
        # Extract target hash from path like /page/profile/<node_hash>
        parts = path.split("/")
        target_hash = parts[-1] if len(parts) > 2 else ""

        if target_hash != self.node_hash:
            return b"Profile not found"

        # Build micron/markdown profile page
        lines = [
            "# %s" % (self.username or "user"),
            "",
            "Node: %s" % self.node_hash,
            "Delivery: %s" % (self.delivery_hash or ""),
            "",
        ]
        # Reference the original pfp image served from this node's /file handler
        orig_name = getattr(self, "my_avatar_orig", None)
        if orig_name:
            lines.append("![avatar](/file/%s)" % os.path.basename(orig_name))
            lines.append("")
        lines.append("Reticulum-native community hub client")
        lines.append("")
        return ("\n".join(lines)).encode("utf-8")

    def _file_handler(self, path, data, request_id, link_id,
                      remote_identity, requested_at):
        """Serve image files from this client's pfps directory.
        Called by remote clients via RNS.Link.request("/file/<filename>").
        Returns raw image bytes.
        """
        parts = path.split("/")
        filename = parts[-1] if len(parts) > 1 else ""
        if not filename:
            return b"Not found"

        app_dir = _app_data_dir()
        pfp_dir = os.path.join(app_dir, "pfps")
        file_path = os.path.join(pfp_dir, filename)

        # Only serve files from the pfps directory (path traversal guard)
        if not os.path.isfile(file_path):
            return b"Not found"

        with open(file_path, "rb") as f:
            return f.read()

    def open_file_chooser(self, avatar=False, purpose=None):
        """Open a file chooser popup to attach a file or select an avatar.
        purpose (optional): "icon" or "banner" routes the picked file to
        the server config screen's upload flow (P10: device uploads)."""
        self._avatar_mode = avatar
        self._picker_purpose = purpose
        if self._android_platform():
            self._android_pick_generic(avatar)
            return
        chooser = FileChooserListView(filters=["*.png", "*.jpg", "*.jpeg",
                                               "*.gif", "*.webp", "*.mp4",
                                               "*.mp3", "*.pdf", "*.txt"])
        popup = Popup(title="Select file", content=chooser,
                      size_hint=(0.9, 0.9))
        chooser.bind(on_submit=lambda inst, selection, *args:
                     self._on_file_selected(selection, popup))
        import ui_fx
        ui_fx.popup_entrance(popup)

    def _route_purpose_file(self, path, purpose):
        """P10: hand the picked file to the server config SCREEN's
        icon/banner upload flow (blobs_put -> admin_config).

        0.36.25 (tay, device): this used to fetch tab_content["General"]
        and call _apply_icon_file on the TAB -- only the SCREEN carries
        those methods, so every Android pick died with an AttributeError
        swallowed by _on_file_selected's except: icon/banner stayed
        white, nothing uploaded. The desktop path was already correct
        (ui_server_config _icon_file_selected calls screen._apply...).
        """
        target = None
        try:
            root = App.get_running_app().root
            target = root.get_screen("server_config") if root else None
        except Exception:
            target = None
        if target is None:
            self._set_status("no server config screen to apply %s"
                             % purpose)
            return
        if purpose == "icon":
            target._apply_icon_file(path)
        else:
            target._apply_banner_file(path)

    def _on_file_selected(self, selection, popup):
        if popup is not None:
            popup.dismiss()
        if not selection:
            return
        path = selection[0]
        try:
            purpose = getattr(self, "_picker_purpose", None)
            if purpose in ("icon", "banner"):
                self._set_status("reading %s..." % purpose)
                self._route_purpose_file(path, purpose)
            elif getattr(self, "_avatar_mode", False):
                self._set_status("opening PFP editor ...")
                self._open_pfp_editor(path)
            else:
                self.attach_file(path)
        except Exception as e:
            import traceback
            dlog("on_file_selected failed: %s" % traceback.format_exc())
            self._set_status("pick routing failed: %s: %s"
                             % (type(e).__name__, e))

    def _open_pfp_editor(self, path):
        """Open round-mask PFP picker popup — drag circle to position, drag outside to resize.

        Save produces: 256x256 PNG from circle area (chat/members) + original (profile top).

        """
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button

        editor = PFPEditorWidget(source=path)

        btn_box = BoxLayout(
            size_hint_y=None,
            height=dp(44),
            spacing=dp(10),
        )
        save_btn = Button(text="Save PFP",
                          size_hint_y=None, height=scaled(dp(44)),
                          pos_hint={"center_y": 0.5},
                          background_color=self.theme_colors['primary'],
                          color=self.theme_colors['btn_text'])
        cancel_btn = Button(text="Cancel",
                            size_hint_y=None, height=scaled(dp(44)),
                            pos_hint={"center_y": 0.5},
                            background_color=self.theme_colors['sidebar_ctrl'],
                            color=self.theme_colors['text_muted'])
        btn_box.add_widget(save_btn)
        btn_box.add_widget(cancel_btn)

        content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(10))
        content.add_widget(editor)
        content.add_widget(btn_box)

        popup = Popup(
            title="Select PFP area — drag circle to move, drag edge to resize",
            content=content,
            size_hint=(0.85, 0.85),
            auto_dismiss=False,
        )

        def do_save(*_):
            # Capture all values BEFORE dismissing popup — once dismissed,
            # the FloatLayout tears down and editor.width/height/img.pos/
            # img.size may become (0, 0) or stale, producing wrong crops.
            #
            # crop_cx/crop_cy/crop_r are the circle in ABSOLUTE window pixels
            # (the exact values the overlay ellipse is drawn with) and
            # img.pos/img.size are the image widget's ABSOLUTE rect — the two
            # live in the same coordinate space, so the crop maths below can
            # map one onto the other directly.
            img = editor.ids.pfp_img
            crop_cx = editor.crop_cx
            crop_cy = editor.crop_cy
            crop_r = editor.crop_r
            image_pos = tuple(img.pos)
            image_size = tuple(img.size)
            popup.dismiss()

            def _do_save(dt):
                self._save_pfp_crop(
                    path=path,
                    crop_cx=crop_cx,
                    crop_cy=crop_cy,
                    crop_r=crop_r,
                    image_pos=image_pos,
                    image_size=image_size,
                )

            Clock.schedule_once(_do_save, 0)

        def do_cancel(*_):
            popup.dismiss()

        def _trace_dismiss(*a):
            # v0.19: the editor flashed-and-closed on tay's phone with no
            # error; kivy has no caller attribution, so print the live
            # dismiss stack: top frames name the file/line that closed it.
            import traceback
            fr = traceback.format_stack(limit=6)
            dlog("editor popup dismissed; stack:\n" + "".join(fr))
            top = fr[-2].strip() if len(fr) > 1 else "?"
            Clock.schedule_once(
                lambda dt: self._set_status(
                    "editor closed by: %s" % top), 0)

        save_btn.bind(on_press=do_save)
        cancel_btn.bind(on_press=do_cancel)
        popup.bind(on_dismiss=_trace_dismiss)
        import ui_fx
        ui_fx.popup_entrance(popup)

        def _check_image(dt):
            try:
                pimg = editor.ids.get("pfp_img")
            except Exception:
                pimg = None
            if pimg is None:
                self._set_status("editor: no pfp_img child (kv rule "
                                 "missing)")
            elif not pimg.texture:
                self._set_status("editor: image not loaded: %s"
                                 % os.path.basename(path))
        Clock.schedule_once(_check_image, 2.0)

    def _restore_avatar_from_disk(self):
        """Bind the newest saved pfp after a restart (tay: profile pfp
        changes did not survive a client restart on Android). The crop
        files were written to pfps/ but nothing ever re-read them, so
        the last session's avatar vanished until a fresh upload.
        """
        if getattr(self, "my_avatar_small", None):
            return
        try:
            pfp_dir = os.path.join(_app_data_dir(), "pfps")
            candidates = [
                os.path.join(pfp_dir, name)
                for name in os.listdir(pfp_dir)
                if name.startswith("small_") and name.endswith(".png")
            ]
            candidates = [c for c in candidates if os.path.isfile(c)]
        except Exception:
            return
        if not candidates:
            return
        newest = max(candidates, key=os.path.getmtime)
        self.my_avatar_small = newest
        self.avatar_preview = newest
        self.composer_pfp_path = newest

    def _save_pfp_crop(self, path, crop_cx=0, crop_cy=0, crop_r=0,
                       image_pos=None, image_size=None):
        """Save the cropped avatar without ever letting it kill the client.

        Any failure in the image pipeline (unreadable file, unwritable
        directory, unsupported format) is caught, logged with a traceback and
        surfaced in the status line. An unhandled exception here used to
        propagate out of the Clock callback, tear down the event loop and
        terminate the whole client with "cannot write mode RGBA as JPEG"
        style errors.
        """
        try:
            self._save_pfp_crop_impl(path, crop_cx, crop_cy, crop_r,
                                     image_pos, image_size)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._set_status("avatar save failed: %s" % e)

    def _save_pfp_crop_impl(self, path, crop_cx=0, crop_cy=0, crop_r=0,
                            image_pos=None, image_size=None):
        """Save 256x256 crop from circle area (chat/members pfp) + original (profile top).

        Both stored in app data dir under pfps/; 256x256 uploaded to guild.

        crop_cx/crop_cy/crop_r are the crop circle in absolute window pixels —
        the same values the editor draws its overlay ellipse with — and
        image_pos/image_size are the image widget's absolute rect. Both are in
        the same (window) coordinate space, so the circle can be mapped onto
        the image by a plain subtraction before converting to natural pixels.

        The old code mixed spaces: it took the circle centre as
        circle_x * editor_w (editor-relative) and subtracted the image's
        absolute pos, which shifted every crop by the editor's position inside
        the popup — always towards the lower-left of the source image.
        """
        try:
            pil = PILImage.open(path).convert("RGBA")
        except Exception:
            # PIL not available — use original directly
            pil = None

        app_dir = _app_data_dir()
        pfp_dir = os.path.join(app_dir, "pfps")
        os.makedirs(pfp_dir, exist_ok=True)

        name = os.path.basename(path)
        # Always use .png extension for saved pfps — PIL infers format
        # from extension and cannot save RGBA as JPEG.
        base = os.path.splitext(name)[0]
        orig_path = os.path.join(pfp_dir, "orig_" + base + ".png")
        small_path = os.path.join(pfp_dir, "small_" + base + ".png")

        if pil is not None:
            # Save original (full-size, used at top of profile page)
            pil.save(orig_path, "PNG")

            # Image widget rect (absolute window pixels)
            img_x, img_y = image_pos if image_pos else (0, 0)
            img_w, img_h = image_size if image_size else (pil.width, pil.height)

            # The image is uniformly scaled (keep_ratio=True):
            # screen_pixels = natural_pixels * scale
            scale = img_w / pil.width if pil.width > 0 else 1.0

            # Circle centre relative to the image's bottom-left corner,
            # in screen pixels (both operands are absolute window coords).
            cx_img_screen = crop_cx - img_x
            cy_img_screen = crop_cy - img_y

            # Convert screen pixels -> natural image pixels (divide by scale)
            cx_px = cx_img_screen / scale if scale > 0 else cx_img_screen
            cy_px = cy_img_screen / scale if scale > 0 else cy_img_screen

            # Circle radius in screen pixels -> natural pixels
            r_px = crop_r / scale if scale > 0 else crop_r

            # Crop from original image (PIL uses top-left origin)
            left = int(cx_px - r_px)
            right = int(cx_px + r_px)
            # Kivy uses bottom-left origin; PIL uses top-left origin
            top = int(pil.height - (cy_px + r_px))
            bottom = int(pil.height - (cy_px - r_px))

            # Clamp to image bounds
            left = max(0, min(left, pil.width))
            right = max(0, min(right, pil.width))
            top = max(0, min(top, pil.height))
            bottom = max(0, min(bottom, pil.height))

            if right > left and bottom > top:
                crop = pil.crop((left, top, right, bottom))
            else:
                # Fallback: center crop
                w, h = pil.size
                size = min(w, h, 256)
                left = (w - size) // 2
                top = (h - size) // 2
                crop = pil.crop((left, top, left + size, top + size))

            crop = crop.resize((256, 256), PILImage.LANCZOS)
            # Apply circular mask — corners become transparent
            mask = PILImage.new("L", (256, 256), 0)
            mask_draw = PILImageDraw.Draw(mask)
            mask_draw.ellipse([0, 0, 255, 255], fill=255)
            crop = crop.convert("RGBA")
            r, g, b, a = crop.split()
            a = PILImageChops.multiply(a, mask)
            crop = PILImage.merge("RGBA", (r, g, b, a))
            crop.save(small_path, "PNG")

            # v0.26: the local display copy stays PNG (alpha circle), but
            # the UPLOAD copy is JPEG over the theme background: a 256x256
            # RGBA PNG inside an RNS link request is 60-120 KB and
            # monopolizes the link while its resource transfer runs, which
            # stalled every other request behind it (tay: "setting pfp
            # appears to block all networking"). Corners composite to the
            # theme bg and the app masks to a circle for display anyway.
            # v0.27: 128x128 upload copy. The v0.26 256px JPEG (~15-20KB)
            # still moved as a multi-packet resource that outran the
            # poll's window (tay: "Poll timeout, upload failed. Happens
            # in under 5 seconds"). A quarter of the pixel area is a
            # 3-6 KB resource -- the avatar rides between poll packets.
            # Display copies stay 256px local PNG.
            up_path = os.path.join(pfp_dir, "up_" + base + ".jpg")
            try:
                up_img = PILImage.new("RGBA", (128, 128), (26, 26, 26, 255))
                mini = crop.resize((128, 128), PILImage.LANCZOS)
                up_img.alpha_composite(mini)
                up_img.convert("RGB").save(up_path, "JPEG", quality=75)
            except Exception:
                up_path = small_path
        else:
            import shutil
            shutil.copy(path, orig_path)
            shutil.copy(path, small_path)
            up_path = small_path

        # Upload 256x256 version to guild as avatar. avatar_upload() blocks
        # on the RNS link, so do it on a worker thread; the local files are
        # saved either way and applied to the UI on the main thread.
        def upload_work():
            try:
                with open(up_path, "rb") as f:
                    blob = f.read()
            except Exception:
                blob = None
            # v0.27: name the failure + retry once -- a fast "request
            # failed" often means the link's queue was mid-poll; after a
            # short drain the same small resource rides through.
            attempt = 0
            last = "no response"
            if blob is None:
                last = "unable to read upload copy"
                attempt = 2
            while attempt < 2:
                try:
                    res = self.guild_api.avatar_upload(blob)
                    confirmed = bool(res and res.get("ok")
                                     and not res.get("unconfirmed"))
                    if not confirmed and res and res.get("unconfirmed"):
                        # A bool receipt carries nothing pollable; do NOT
                        # retry blind (that re-uploads and can double-send).
                        # Verify by reading the avatar back from the guild;
                        # only a real state mismatch counts as failure.
                        try:
                            got = self.guild_api.avatar_get(self.my_hash or "")
                            confirmed = (got or {}).get("avatar") == blob
                            if not confirmed:
                                last = "read-back mismatch"
                        except Exception as e:
                            last = str(e)[:48]
                    if confirmed:
                        def ok(dt):
                            self._set_status("Avatar updated (%d KB)"
                                             % (len(blob) // 1024))
                        Clock.schedule_once(ok, 0)
                        return
                    last = str((res or {}).get("error") or "no response")[:48]
                except Exception as e:
                    last = str(e)[:48]
                attempt += 1
                if attempt < 2:
                    time.sleep(2.0)

            def fail2(dt):
                self._set_status("avatar upload failed: %s" % last)
            Clock.schedule_once(fail2, 0)
        threading.Thread(target=upload_work, daemon=True).start()

        # Always store paths locally regardless of guild API result —
        # the files are saved to disk even if guild upload fails.
        self.my_avatar_orig = orig_path
        self.my_avatar_small = small_path
        # tay (2026-11): the onboarding slot and the connect gate bind
        # to avatar_preview.
        self.avatar_preview = small_path

        # Apply to profile screen if it exists
        if hasattr(self, "profile_screen"):
            self.profile_screen.target_avatar = small_path
            self.profile_screen.ids.profile_pfp.source = small_path

        # task 9: the composer pfp follows the saved crop immediately
        try:
            self.composer_pfp_path = small_path
        except Exception:
            pass

        # Refresh existing chat message rows for local user with new pfp
        msg_widgets = getattr(self, "_msg_widgets", {})
        for row in msg_widgets.values():
            if getattr(row, "is_own", False):
                row.avatar_source = small_path

        # Force re-render of own avatar in IMAGE_CACHE so new messages pick it up
        if self.my_hash:
            key = "av:" + self.my_hash
            with open(small_path, "rb") as f:
                IMAGE_CACHE.set_bytes(key, f.read(), "png")

    def open_confirm_delete_account(self, *_):
        """P1 (tay 2026-11-19): purge this guild account from the server.

        Two stage-gates matching the server's own validations verbatim
        (account/delete-posts and account/leave check the confirm
        strings exactly): "DELETE MY POSTS", then "LEAVE GUILD".
        Between the two, only the server mutates; the local wipe runs
        after leave confirms so a failed step never strands the local
        profile.
        """
        from kivymd.uix.button import MDButton, MDButtonText
        if not (self.guild_api or getattr(self, "guild_api", None)):
            self._set_status("No guild connection: nothing to purge.")
            return
        box = BoxLayout(orientation="vertical", spacing=dp(10),
                        padding=dp(12))
        note = Label(text=("Delete account erases everything you posted "
                           "to this guild -- messages, edits, reactions "
                           "and attachment files -- then leaves the "
                           "guild. This cannot be undone."),
                     color=self.theme_colors["text_muted"],
                     font_size=sp(13), size_hint_y=None, height=dp(70),
                     halign="center", valign="middle")
        note.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        box.add_widget(note)
        typed = TextInput(multiline=False, hint_text="Type DELETE MY POSTS",
                          font_name="MeshBody", font_size=sp(16),
                          foreground_color=self.theme_colors["text"],
                          hint_text_color=self.theme_colors["text_muted"],
                          background_normal="", background_active="",
                          background_color=self.theme_colors["input_bg"],
                          size_hint_y=None, height=dp(46))
        box.add_widget(typed)
        status = Label(text="", color=self.theme_colors["accent"],
                       size_hint_y=None, height=dp(22))
        box.add_widget(status)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(10))
        box.add_widget(row)
        go = MDButton(style="filled", theme_bg_color="Custom",
                      md_bg_color=(0.76, 0.22, 0.22, 1),
                      size_hint=(None, None), width=dp(160))
        go.add_widget(MDButtonText(text="Erase my posts",
                                   text_color=(1, 1, 1, 1)))
        stay = MDButton(style="outlined", size_hint=(None, None),
                        width=dp(120))
        stay.add_widget(MDButtonText(text="Cancel",
                                     text_color=self.theme_colors["text"]))
        row.add_widget(go)
        row.add_widget(stay)
        sheet = Popup(title="Stage 1 of 2: erase posts", content=box,
                      size_hint=(0.9, 0.62))

        def proceed(*_a):
            if (typed.text or "") != "DELETE MY POSTS":
                status.text = "The phrase does not match."
                return
            sheet.dismiss()
            self.open_confirm_leave_guild()

        go.bind(on_press=proceed)
        stay.bind(on_press=lambda *_: sheet.dismiss())
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def open_confirm_leave_guild(self, *_):
        """Stage 2: the literal the leave endpoint validates."""
        from kivymd.uix.button import MDButton, MDButtonText
        box = BoxLayout(orientation="vertical", spacing=dp(10),
                        padding=dp(12))
        note = Label(text=("Posts erased. Leaving releases your "
                           "membership, roles, receipts and registration "
                           "on this guild. Type LEAVE GUILD to finish."),
                     color=self.theme_colors["text_muted"],
                     font_size=sp(13), size_hint_y=None, height=dp(64),
                     halign="center", valign="middle")
        note.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        box.add_widget(note)
        typed = TextInput(multiline=False, hint_text="Type LEAVE GUILD",
                          font_name="MeshBody", font_size=sp(16),
                          foreground_color=self.theme_colors["text"],
                          hint_text_color=self.theme_colors["text_muted"],
                          background_normal="", background_active="",
                          background_color=self.theme_colors["input_bg"],
                          size_hint_y=None, height=dp(46))
        box.add_widget(typed)
        status = Label(text="", color=self.theme_colors["accent"],
                       size_hint_y=None, height=dp(22))
        box.add_widget(status)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(10))
        box.add_widget(row)
        go = MDButton(style="filled", theme_bg_color="Custom",
                      md_bg_color=(0.76, 0.22, 0.22, 1),
                      size_hint=(None, None), width=dp(160))
        go.add_widget(MDButtonText(text="Leave guild",
                                   text_color=(1, 1, 1, 1)))
        stay = MDButton(style="outlined", size_hint=(None, None),
                        width=dp(120))
        stay.add_widget(MDButtonText(text="Cancel",
                                     text_color=self.theme_colors["text"]))
        row.add_widget(go)
        row.add_widget(stay)
        sheet = Popup(title="Stage 2 of 2: leave guild", content=box,
                      size_hint=(0.9, 0.62))

        def proceed(*_a):
            if (typed.text or "") != "LEAVE GUILD":
                status.text = "The phrase does not match."
                return
            sheet.dismiss()
            self.purge_account_worker()

        go.bind(on_press=proceed)
        stay.bind(on_press=lambda *_: sheet.dismiss())
        import ui_fx
        ui_fx.popup_entrance(sheet)

    def purge_account_worker(self, *_):
        """Both server steps, in order, on the worker thread; every
        failure aborts before the local wipe (the account stays
        functional and the retries are idempotent)."""
        api = self.guild_api
        if api is None:
            return

        def work():
            res1 = None
            res2 = None
            try:
                res1 = api.account_delete_posts(
                    {"confirm": "DELETE MY POSTS"})
            except Exception as exc:
                print("account delete-posts failed:", repr(exc))
            if not (res1 or {}).get("ok"):
                Clock.schedule_once(
                    lambda dt: self._set_status(
                        "Post purge failed: nothing was deleted locally."),
                    0)
                return
            try:
                res2 = api.account_leave({"confirm": "LEAVE GUILD"})
            except Exception as exc:
                print("account leave failed:", repr(exc))
            if not (res2 or {}).get("ok"):
                Clock.schedule_once(
                    lambda dt: self._set_status(
                        "Leave failed: guild membership intact."),
                    0)
                return
            Clock.schedule_once(
                lambda dt: self.finish_account_purge(), 0)

        threading.Thread(target=work, daemon=True).start()

    def finish_account_purge(self):
        """Local wipe + gate back to staging: the two profile files
        (their existence is the routing gate at boot), my cached
        avatar rows, avatar/username state. The RNS identity file and
        servers.json survive: the identity outlives one guild, and the
        hub entry lets a fresh account reconnect.
        """
        for path in (self._portable_profile_path(),
                     self._onboarding_path(),
                     # the saved session carries hub login state; without
                     # this the next boot would resume main with a purged
                     # account instead of staging fresh
                     self._session_path()):
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except OSError as e:
                print("profile file delete failed:", repr(e))
        key = "av:" + (getattr(self, "my_hash", "") or "")
        IMAGE_CACHE.forget(key)
        self.my_avatar_small = ""
        self.avatar_preview = ""
        self.username = ""
        self._portable_name = ""
        self.i_am_admin = False
        self._set_status("Account purged: stage a new profile to return.")
        try:
            if self.root is not None:
                self.root.get_screen("onboarding").stage = 0
                self.root.current = "onboarding"
        except Exception as e:
            print("routing back to staging failed:", repr(e))

    def save_profile(self):
        """Save username to the guild."""
        try:
            inp = self.root.get_screen("profile").ids.profile_username_input
            new_name = (inp.text or "").strip()
            # An empty field means "keep the current name". Falling back
            # to the literal "user" would try to claim a name that the
            # first client to save it took for good (the guild answers
            # 409 "username taken"). The unique default is the right
            # fallback, and it matches what the guild already shows.
            if not new_name:
                new_name = self.username or default_username(self.my_hash)
        except Exception as e:
            import traceback
            dlog("save_profile pre-register failed: %s"
                 % traceback.format_exc())
            self._set_status("save failed (input read): %s: %s"
                             % (type(e).__name__, e))
            return

        # register() blocks on the RNS link — keep it off the main thread.
        self._set_status("registering \"%s\" ..." % new_name)

        def work():
            try:
                res = self.guild_api.register(new_name, "")
            except Exception as e:
                err = "save failed: %s: %s" % (type(e).__name__, e)
                dlog("save_profile register raised: %s" % repr(e))
                Clock.schedule_once(
                    lambda dt: self._set_status(err), 0)
                return
            dlog("save_profile register response: %r" % (res,))
            if not isinstance(res, dict):
                Clock.schedule_once(
                    lambda dt: self._set_status(
                        "save failed: bad response %r" % (res,)), 0)
                return
            if res.get("ok"):
                def apply(dt):
                    self.username = new_name
                    try:
                        self.profile_screen.target_username = new_name
                    except Exception:
                        pass
                    # persist: restarts load the session file
                    try:
                        self._save_session()
                    except Exception:
                        pass
                    self._set_status("Profile saved: %s" % new_name)
                # v0.31.6: a register can grant the Founder role (first
                # member of a server) — refresh the role view so the
                # channel-management affordances appear without a restart.
                self._fetch_whoami()
                Clock.schedule_once(apply, 0)
            else:
                def fail(dt):
                    self._set_status(
                        "save failed: %s" % res.get("error", "unknown"))
                    try:
                        inp.helper_text = (
                            "server: %s" % str(res.get("error"))[:64])
                    except Exception:
                        pass
                Clock.schedule_once(fail, 0)
        threading.Thread(target=work, daemon=True).start()

    # -- ui helpers ------------------------------------------------------
    def _clear_fetch_status(self):
        """Clear a stale 'fetch failed' status after a later success.

        H6: _fetch_messages only ever set the status on failure, so a
        transient timeout stuck on the line forever. Only a fetch
        failure is cleared -- any other status is left alone.
        """
        try:
            lbl = self.root.get_screen("connect").ids.status
            if (lbl.text or "").startswith("fetch failed"):
                lbl.text = ""
        except Exception:
            pass
        try:
            lbl = self.root.get_screen("profile").ids.get("profile_status")
            if lbl is not None and (lbl.text or "").startswith(
                    "fetch failed"):
                lbl.text = ""
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.root.get_screen("connect").ids.status.text = text
        except Exception:
            pass
        # v0.25: the connect screen is usually not on display. Mirror
        # every status onto the profile screen's own line -- tay's
        # v0.24 receipt ("name change does not actually do anything")
        # was the register verdicts rendering on an invisible screen.
        try:
            ps = self.root.get_screen("profile")
            lbl = ps.ids.get("profile_status")
            if lbl is not None:
                lbl.text = str(text)
        except Exception:
            pass

    def on_resume(self):
        """v0.31.5: the app came back (SAF pick return, any foreground).
        Android stopped the activity underneath us; heal everything the
        stop may have disturbed instead of demanding a restart."""
        self._heal_after_resume()

    def on_stop(self):
        # v0.28: push replaced the poller/streamer/presence-fetcher.
        # v0.29: nothing pings, so there is no tick loop left to stop.
        # v0.31.5 (tay: "We don't *want* to stop the app"): Android fires
        # on_stop for EVERY opaque-surface activity stop -- i.e. every
        # SAF file pick -- not just real exits, and swipe-away terminates
        # the process without running this at all. Tearing state down
        # here froze DMs and console work mid-session on the phone.
        # Desktop closes are real closes: keep the tidy teardown there
        # only. Android: leave everything running.
        from RNS.vendor.platformutils import is_android as _is_android
        if _is_android():
            self._term_sessions.clear()
            return
        # close any open console sessions so they don't idle out server-side.
        # Each term_close() blocks on the RNS link for up to the request
        # timeout, so doing them inline freezes shutdown for N * timeout
        # seconds. Close them on a background thread with a short timeout
        # instead — the sessions are server-side garbage-collected anyway.
        sessions = list(self._term_sessions.values())
        if sessions:
            api = self.guild_api
            if api is not None:
                def close_all():
                    for sid in sessions:
                        try:
                            api.term_close(sid, timeout=1.5)
                        except Exception:
                            pass
                t = threading.Thread(target=close_all, daemon=True)
                t.start()
                t.join(2.0)
        self._term_sessions.clear()
        # shut the LXMF router so its announce/delivery threads don't leak
        if self.lxmf_router is not None:
            try:
                self.lxmf_router.exit_handler()   # LXMF's own teardown hook
            except Exception:
                pass
            self.lxmf_router = None
            self.lxmf_delivery = None
        # desktop teardown carries voice state off with it
        try:
            VoicePanelMixin.on_stop(self)
        except Exception:
            pass


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not KIVY:
        print("Kivy is required to run the client: pip install kivy[base]", file=sys.stderr)
        print(_KIVY_IMPORT_ERROR)
        sys.exit(1)

    # --debug enables verbose attachment/ingest logging on the client.
    import argparse as _argparse
    _dbg_parser = _argparse.ArgumentParser(add_help=False)
    _dbg_parser.add_argument("--debug", action="store_true")
    _dbg_args, _ = _dbg_parser.parse_known_args()
    DEBUG = _dbg_args.debug
    # Phase 2 split: dlog lives in client_util now; keep its flag
    # in step so mixin dlog() calls honour --debug too.
    client_util.DEBUG = DEBUG

    # Graceful GL backend fallback: Wayland without a GPU, headless runners,
    # and VMs often fail with "No matching FB config found" from SDL2/OpenGL.
    # Try software GL first, then the mock backend as last resort.
    _attempt = int(os.environ.get("_SHades_GL_ATTEMPT", "1"))
    try:
        sHadesClientApp().run()
    except Exception as _e:
        _msg = str(_e)
        if _attempt < 3 and (
            "FB config" in _msg
            or "No matching" in _msg
            or "couldn't connect to X server" in _msg
        ):
            os.environ["_SHades_GL_ATTEMPT"] = str(_attempt + 1)
            if _attempt == 1:
                os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
            else:
                os.environ["KIVY_GL_BACKEND"] = "mock"
            os.execv(sys.executable, [sys.executable] + sys.argv[1:])
        # The app may have been running for a long time before this error —
        # print the traceback so the real cause is visible instead of a
        # one-line message that claims the client "failed to start".
        import traceback
        traceback.print_exc()
        print(f"sHades client crashed: {_e}", file=sys.stderr)
        sys.exit(1)
