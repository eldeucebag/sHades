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
import threading
import random
from collections import OrderedDict
import mimetypes
from PIL import Image as PILImage
from PIL import ImageDraw as PILImageDraw
from PIL import ImageChops as PILImageChops

from ui_scale import scaled

# ---------------------------------------------------------------------------
# Kivy (guarded so the module can be imported for linting without Kivy present)
# ---------------------------------------------------------------------------
try:
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.label import Label
    from kivy.uix.image import Image, AsyncImage
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.screenmanager import ScreenManager, Screen
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
    # when Kivy is absent. Each name gets its own distinct stub class:
    # the client stacks several of them in the same bases tuple, and two
    # occurrences of one class make that definition a TypeError. They are
    # import-only: instantiating one raises, so nothing silently pretends
    # to be a widget. Client test suites that need real widgets already
    # SKIP on their own via kc.KIVY.
    def _make_kivy_stub(name):
        class _Stub(object):
            def __init__(self, *args, **kwargs):
                raise RuntimeError(
                    "Kivy stub class %s instantiated; Kivy is not "
                    "available (%r)" % (name, _KIVY_IMPORT_ERROR))
        _Stub.__name__ = name
        return _Stub

    FloatLayout = _make_kivy_stub("FloatLayout")
    BoxLayout = _make_kivy_stub("BoxLayout")
    Label = _make_kivy_stub("Label")
    ButtonBehavior = _make_kivy_stub("ButtonBehavior")
    App = _make_kivy_stub("App")
    MDApp = _make_kivy_stub("MDApp")

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
        KIVYMD = False
        _KIVYMD_IMPORT_ERROR = _e
else:
    KIVYMD = False

# ---------------------------------------------------------------------------
# TEMP boot diagnostics (v0.3): on-screen heartbeat overlay.
# Prints lifecycle milestones in a plain-Kivy label so a launch failure is
# visible in a screenshot with no adb. The overlay auto-hides 90s after the
# window appears. Remove BOOT_DEBUG once the launch diagnosis is done.
# ---------------------------------------------------------------------------
BOOT_DEBUG = True

_boot_lines = []
_boot_label = {"ref": None}


def _boot_mark(msg):
    """Record a boot milestone; drawn onto the overlay once it exists."""
    if not BOOT_DEBUG:
        return
    _boot_lines.append(msg)
    _boot_refresh()


def _boot_refresh():
    lbl = _boot_label["ref"]
    if lbl is not None:
        lbl.text = "\n".join(_boot_lines)


def _boot_install(screen):
    """Attach the heartbeat INSIDE the screen's root layout (kept as a child
    of the root box so the screen's structural contract -- status label as a
    direct child of children[0], etc. -- stays intact); auto-hides."""
    from kivy.clock import Clock

    lbl = Label(
        text="\n".join(_boot_lines), color=(1, 0.85, 0.4, 1),
        font_size="10sp", halign="left", valign="top", markup=False,
        size_hint=(None, None), pos_hint={"x": 0.01, "top": 0.995},
    )
    lbl.bind(texture_size=lambda *a: setattr(
        lbl, "size", (min(lbl.texture_size[0], 420), lbl.texture_size[1])))
    host = screen.children[0] if screen.children else screen
    host.add_widget(lbl)  # inside the root layout, top-left via pos_hint
    _boot_label["ref"] = lbl
    lbl.text = "\n".join(_boot_lines)

    Clock.schedule_once(lambda dt: _boot_hide(), 90)


def _boot_reattach(screen):
    """Move the heartbeat onto the newly-current screen (e.g. main)."""
    lbl = _boot_label["ref"]
    if lbl is None:
        return
    if lbl.parent is not None:
        lbl.parent.remove_widget(lbl)
    host = screen.children[0] if screen.children else screen
    host.add_widget(lbl)
    lbl.text = "\n".join(_boot_lines)


def _boot_hide():
    lbl = _boot_label["ref"]
    if lbl is not None and lbl.parent is not None:
        lbl.parent.remove_widget(lbl)
    _boot_label["ref"] = None


import RNS  # noqa: E402  (after the guarded kivy/diagnostics block, by design)
from RNS.vendor import umsgpack as msgpack  # noqa: E402
import invite  # noqa: E402
import dms  # noqa: E402
import ui_geom  # noqa: E402
from micron import (micron_parse, micron_page_bg, micron_media_target,  # noqa: E402
                    runs_to_markup)

# Attachment storage styles:
#   server   - file is stored on the guild; bytes re-fetched via /file/attachments
#              path (survives restart, shown with a loading placeholder until received).
#   ephemeral - file is sent inline as [name, mime, blob] (p2p); bytes kept locally
#              so they survive restart, but there is no server copy to re-fetch.
ATT_SERVER = "server"
ATT_EPHEMERAL = "ephemeral"
ATT_PLACEHOLDER_TEXT = "[loading image…]"

# Default UI palette — the single source of truth for theme colors. The app
# seeds theme_colors from this, the config screen's Reset restores it, and
# headless callers install it on a bare App so KV's app.theme_colors[...]
# expressions resolve (THEME_KEYS + THEME_LABELS drive the config screen's
# generated colour rows).
DEFAULT_THEME_COLORS = {
    "bg_main": (0.04, 0.04, 0.04, 1),      # screens / root background - near black
    "panel": (0.07, 0.07, 0.07, 1),        # center chat area
    "panel_darker": (0.09, 0.09, 0.09, 1),  # input bar / dropdowns
    "bg_sidebar": (0.05, 0.05, 0.05, 1),   # side window backgrounds
    "sidebar_ctrl": (0.08, 0.08, 0.08, 1),  # sidebar buttons / slim bars
    "topbar": (0.04, 0.04, 0.04, 1),       # top bar background - near black
    "primary": (0.87, 0.60, 0.23, 1),      # primary buttons - vintage amber #DF993A
    "accent": (0.0, 0.75, 0.65, 1),        # accent / destructive - vibrant teal #00BFA6
    "text": (0.87, 0.60, 0.23, 1),         # body text - vintage amber
    "text_muted": (0.72, 0.53, 0.18, 1),   # secondary text - aged amber #B8862D
    # #B08C46 -- the old #997026 measured 4.19:1 on the chat surface and
    # 3.90:1 on the focused input fill, both below the 4.5:1 WCAG AA
    # threshold for body text. This value measures 5.96:1 / 5.54:1.
    "text_dim": (0.690, 0.549, 0.275, 1),   # dimmer secondary text
    "input_bg": (0.09, 0.09, 0.09, 1),     # text field fill
    "input_bg_focus": (0.11, 0.11, 0.11, 1),
    # #1E1E1E -- msg_own used to equal the panel colour exactly, so the
    # own-message bubble was invisible against the chat surface.
    "msg_own": (0.118, 0.118, 0.118, 1),   # own-message bubble / embeds
    "btn_text": (0.04, 0.04, 0.04, 1),     # button text - near black (for amber buttons)
    # #646464 -- first value that clears 3:1 against all three dark
    # surfaces (3.35 / 3.26 / 3.17), so input outlines are actually
    # visible. Without a border token, inputs read as flat rectangles.
    "border": (0.392, 0.392, 0.392, 1),    # input outlines, focus rings
    "border_subtle": (0.227, 0.227, 0.227, 1),  # dividers, scrollbar trough
}

THEME_KEYS = (
    "bg_main", "panel", "panel_darker", "bg_sidebar", "sidebar_ctrl",
    "topbar", "primary", "accent", "text", "text_muted", "text_dim",
    "input_bg", "input_bg_focus", "msg_own", "btn_text", "border",
    "border_subtle",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hexstr(b):
    if isinstance(b, str):
        return b
    return RNS.hexrep(b, delimit=False)


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


def now():
    return time.time()


def cache_dir():
    d = os.path.join(_app_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cached_path(key):
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32])


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


def rgba_to_hex(rgba):
    return "#%02x%02x%02x" % (
        int(max(0, min(1, rgba[0])) * 255),
        int(max(0, min(1, rgba[1])) * 255),
        int(max(0, min(1, rgba[2])) * 255),
    )


def hex_to_rgba(text):
    text = (text or "").lstrip("#").strip()
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError("bad hex color: %r" % text)
    return (
        int(text[0:2], 16) / 255.0,
        int(text[2:4], 16) / 255.0,
        int(text[4:6], 16) / 255.0,
        1.0,
    )


# ---------------------------------------------------------------------------
# Markdown -> Kivy markup (NomadNet subset, mirrored with the guild renderer)
# ---------------------------------------------------------------------------
# The guild translates this same message-markdown subset to Micron
# (sHades.md_to_micron_line); the client renders it to Kivy markup instead.
# Subset: **bold**, *italic*, __underline__, `code`, [label](target).
# Links are inert text — there is no HTTP on Reticulum, and micron link
# targets (node hashes, /page/... paths) are not browsable from the Kivy
# client — so no clickable hyperlinks are ever created.
_MD = re.compile(
    r"`(?P<code>[^`\n]{1,120})`"
    r"|\*\*(?P<bold>[^*\n]{1,200})\*\*"
    r"|__(?P<under>[^_\n]{1,200})__"
    r"|\[(?P<label>[^\]\n]{1,128})\]\((?P<target>[^)\s]{1,256})\)"
    r"|\*(?P<i>[^*\n]{1,200})\*"
)


def md_to_kivy(text):
    """Convert the NomadNet message-markdown subset to Kivy markup."""
    if not text:
        return ""
    out = []
    pos = 0
    for m in _MD.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        if m.group("code") is not None:
            out.append("[color=#BFBFBF]" + _escape(m.group("code")) + "[/color]")
        elif m.group("bold") is not None:
            out.append("[b]" + _escape(m.group("bold")) + "[/b]")
        elif m.group("under") is not None:
            out.append("[u]" + _escape(m.group("under")) + "[/u]")
        elif m.group("label") is not None:
            out.append(_escape(m.group("label")) + " (" + _escape(m.group("target")) + ")")
        elif m.group("i") is not None:
            out.append("[i]" + _escape(m.group("i")) + "[/i]")
        pos = m.end()
    out.append(_escape(text[pos:]))
    return "".join(out)


def _escape(s):
    s = s.replace("&", "&" + "amp;")
    s = s.replace("[", "&bl;")
    s = s.replace("]", "&br;")
    return s


def dlog(msg):
    """Debug log — only prints when --debug is passed."""
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


# ---------------------------------------------------------------------------
# API client over RNS link
# ---------------------------------------------------------------------------
class GuildAPI:
    """Thin wrapper around an RNS Link to the sHades guild node."""

    def __init__(self, reticulum, link, on_error=None):
        self.reticulum = reticulum
        self.link = link
        self.on_error = on_error or (lambda *a: None)

    def _request(self, path, data, timeout=10):
        if not isinstance(data, dict):
            data = {}
        receipt = self.link.request(path, data=data)
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                return {"ok": 0, "error": "request failed"}
            # RNS queued the request but gave no receipt — poll for response
            # via a fresh receipt. Create one by calling request again without data.
            receipt = self.link.request(path, data={})
        deadline = time.time() + timeout
        while (hasattr(receipt, "response")
               and receipt.response is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not hasattr(receipt, "response") or receipt.response is None:
            return {"ok": 0, "error": "timeout"}
        try:
            return msgpack.unpackb(receipt.response, raw=False)
        except Exception as e:
            return {"ok": 0, "error": "bad response: %s" % e}

    def state(self, channel):
        return self._request("/api/state", {"channel": channel})

    def messages(self, channel, since_id=None, since_ts=None, before=None, limit=50):
        data = {"channel": channel, "limit": limit}
        if since_id is not None:
            data["since_id"] = since_id
        if since_ts is not None:
            data["since_ts"] = since_ts
        if before is not None:
            data["before"] = before
        return self._request("/api/messages", data)

    def post(self, channel, content, attachments=None):
        data = {"channel": channel, "content": content}
        if attachments:
            data["attachments"] = attachments
        return self._request("/api/post", data)

    def edit(self, msg_id, content):
        return self._request("/api/edit", {"msg": msg_id, "content": content})

    def delete(self, msg_id):
        return self._request("/api/delete", {"msg": msg_id})

    def register_node(self, node_hash, address, port, identity_hash):
        """Register this client's node address with the guild index.

        This is also the presence ping: the client repeats it every
        ANNOUNCE_INTERVAL and the guild reads each call as "still here". No
        identity is asserted in the payload — the guild derives the user hash
        from the identity this link was identified with, so it cannot be forged.
        """
        return self._request("/api/register_node", {
            "node_hash": node_hash,
            "address": address,
            "port": port,
            "identity_hash": identity_hash,
        })

    def lookup_node(self, node_hash):
        return self._request("/api/lookup_node", {"node_hash": node_hash})

    def register(self, username, profile_node=""):
        return self._request("/api/register",
                             {"username": username, "profile_node": profile_node})

    def avatar_upload(self, blob):
        return self._request("/api/avatar", {"avatar": blob})

    def avatar_get(self, user_hash):
        return self._request("/api/avatar_get", {"user": user_hash})

    def nodes(self):
        """Fetch the current active node registry from the guild."""
        return self._request("/api/nodes", {})

    def presence(self):
        """Fetch the active user list — who is currently pinging the guild.

        Stale clients are absent from the response, not flagged, so a caller
        that mirrors this list is pruned automatically.
        """
        return self._request("/api/presence", {})

    # ---- JSON API v1 (json bytes over the same RNS link; no HTTP anywhere) ----

    def _request_json(self, path, data, timeout=10.0):
        payload = json.dumps(data if data is not None else {}).encode("utf-8")
        receipt = self.link.request(path, data=payload)
        # link.request() may return a Receipt object or True/False depending
        # on the RNS version (see _request). Re-issue with the SAME payload.
        if isinstance(receipt, bool):
            if not receipt:
                return {"ok": 0, "error": "request failed"}
            receipt = self.link.request(path, data=payload)
        deadline = time.time() + timeout
        while (hasattr(receipt, "response")
               and receipt.response is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not hasattr(receipt, "response") or receipt.response is None:
            return {"ok": 0, "error": "timeout"}
        try:
            return json.loads(receipt.response.decode("utf-8"))
        except Exception as e:
            return {"ok": 0, "error": "bad response: %s" % e}

    def channels_list(self):
        return self._request_json("/api/v1/channels/list", {})

    def channels_create(self, name, kind="chat", manifest=None):
        data = {"name": name, "kind": kind}
        if manifest is not None:
            data["manifest"] = manifest
        return self._request_json("/api/v1/channels/create", data)

    def channels_info(self, channel):
        return self._request_json("/api/v1/channels/info", {"channel": channel})

    def stream_subscribe(self, channel, cursor=None):
        data = {"channel": channel}
        if cursor is not None:
            data["cursor"] = cursor
        return self._request_json("/api/v1/stream/subscribe", data)

    def stream_poll(self, cursor=None, timeout_s=4.0):
        data = {"timeout_s": timeout_s}
        if cursor is not None:
            data["cursor"] = cursor
        # server may hold the poll for timeout_s; allow RTT on top
        return self._request_json("/api/v1/stream/poll", data, timeout=timeout_s + 8)

    def stream_unsubscribe(self):
        return self._request_json("/api/v1/stream/unsubscribe", {})

    def typing(self, channel, active=True):
        return self._request_json("/api/v1/typing",
                                  {"channel": channel, "active": bool(active)})

    def presence_v1(self):
        """Active users plus the channel each one is streaming."""
        return self._request_json("/api/v1/presence", {})

    def blobs_put(self, name, mime, data):
        """Upload one content-addressed blob (v1). Returns
        {ok, blob_id, size, path} — reference blob_id from post_v1."""
        return self._request_json("/api/v1/blobs/put", {
            "name": name, "mime": mime,
            "data_b64": base64.b64encode(data).decode("ascii"),
        })

    def post_v1(self, channel, content, attachments=None):
        """JSON chat post; attachments are [{blob_id, name?, mime?}] refs or
        inline {name, mime, data_b64} dicts."""
        data = {"channel": channel, "content": content}
        if attachments:
            data["attachments"] = attachments
        return self._request_json("/api/v1/post", data, timeout=30)

    def blobs_get(self, blob_id, offset=0, length=None):
        """Chunked blob read; returns {data_b64, total, offset, eof}."""
        data = {"blob_id": blob_id, "offset": offset}
        if length is not None:
            data["len"] = length
        return self._request_json("/api/v1/blobs/get", data)

    def site_get(self, channel, path, offset=0, length=None):
        """Chunked site resource read (micron pages, images, media)."""
        data = {"channel": channel, "path": path, "offset": offset}
        if length is not None:
            data["len"] = length
        return self._request_json("/api/v1/site/get", data)

    def site_manifest(self, channel):
        return self._request_json("/api/v1/site/manifest", {"channel": channel})

    def term_open(self, channel):
        """Open a guild console session on a term channel."""
        return self._request_json("/api/v1/term/open", {"channel": channel},
                                  timeout=15)

    def term_data(self, session, text):
        """Feed one line (or fragment) of console input."""
        return self._request_json("/api/v1/term/data", {
            "session": session,
            "data_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        })

    def term_close(self, session, timeout=10.0):
        return self._request_json("/api/v1/term/close",
                                  {"session": session}, timeout=timeout)

    def receipts(self, channel, msg_id=None):
        """Read watermark: mark up to msg_id, or report {last_read_id, unread}."""
        data = {"channel": channel}
        if msg_id:
            data["msg_id"] = msg_id
        return self._request_json("/api/v1/receipts", data)

    def react(self, msg_id, emoji):
        """Toggle a reaction (ASCII shortcode)."""
        return self._request_json("/api/v1/react",
                                  {"msg": msg_id, "emoji": emoji})

    def search(self, q, channel=None, limit=None):
        data = {"q": q}
        if channel:
            data["channel"] = channel
        if limit:
            data["limit"] = limit
        return self._request_json("/api/v1/search", data)

    def whoami(self):
        return self._request_json("/api/v1/whoami", {})

    def mod_ban(self, user_hash, reason=""):
        return self._request_json("/api/v1/mod/ban",
                                  {"user": user_hash, "reason": reason})

    def mod_unban(self, user_hash):
        return self._request_json("/api/v1/mod/unban", {"user": user_hash})

    def mod_mute(self, user_hash, reason=""):
        return self._request_json("/api/v1/mod/mute",
                                  {"user": user_hash, "reason": reason})

    def mod_unmute(self, user_hash):
        return self._request_json("/api/v1/mod/unmute", {"user": user_hash})

    def channels_delete(self, channel):
        return self._request_json("/api/v1/channels/delete", {"channel": channel})

    def command(self, line):
        """Slash-command passthrough: runs the shared console dispatcher,
        returns the output ephemerally."""
        return self._request_json("/api/v1/command", {"input": line})

    # ---- roles (U5) ----
    def roles_list(self):
        return self._request_json("/api/v1/roles/list", {})

    def roles_create(self, name, perms=0, color=None, position=0):
        return self._request_json("/api/v1/roles/create",
                                  {"name": name, "perms": perms,
                                   "color": color, "position": position})

    def roles_delete(self, role_id):
        return self._request_json("/api/v1/roles/delete", {"role_id": role_id})

    def roles_assign(self, role_id, user):
        return self._request_json("/api/v1/roles/assign",
                                  {"role_id": role_id, "user": user})

    def roles_unassign(self, role_id, user):
        return self._request_json("/api/v1/roles/unassign",
                                  {"role_id": role_id, "user": user})

    # ---- admin panel (U8) ----
    def admin_config(self, key=None, value=None):
        data = {}
        if key is not None:
            data["key"] = key
        if value is not None:
            data["value"] = value
        return self._request_json("/api/v1/admin/config", data)

    def admin_audit(self, limit=100):
        return self._request_json("/api/v1/admin/audit", {"limit": limit})

    def admin_mods(self, op="list", user=None):
        data = {"op": op}
        if user:
            data["user"] = user
        return self._request_json("/api/v1/admin/mods", data)

    # ---- join gate (U6b) ----
    def join(self, code=None):
        """Join status (no code) or join with an invite code."""
        data = {}
        if code:
            data["code"] = code
        return self._request_json("/api/v1/join", data)

    def invites(self, op="list", code=None, label=None, max_uses=None,
                expires_in=None):
        data = {"op": op}
        if code:
            data["code"] = code
        if label is not None:
            data["label"] = label
        if max_uses is not None:
            data["max_uses"] = max_uses
        if expires_in is not None:
            data["expires_in"] = expires_in
        return self._request_json("/api/v1/invites", data)

    def fetch_file(self, path):
        """Fetch raw bytes from a /file/... request handler on the guild."""
        dlog(f"fetch_file: requesting {path}")
        receipt = self.link.request(path, data={})
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                dlog(f"fetch_file: link.request returned False for {path}")
                return None
            receipt = self.link.request(path, data={})
        deadline = time.time() + 15
        while receipt.response is None and time.time() < deadline:
            time.sleep(0.05)
        if receipt.response is None:
            dlog(f"fetch_file: timeout for {path}")
            return None
        dlog(f"fetch_file: received {len(receipt.response)} bytes for {path}")
        return receipt.response


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


IMAGE_CACHE = ImageCache()


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

    def on_touch_up(self, touch):
        clock = getattr(self, "_ctx_clock", None)
        if clock:
            clock.cancel()
            self._ctx_clock = None
        if touch.ud.get("shades_ctx_fired"):
            # the long-press fired: swallow the corresponding up
            touch.ud["shades_ctx_fired"] = False
            touch.ungrab(self)
            return True
        return super().on_touch_up(touch)


if KIVY:
    class PfpImage(Image):
        """Circular-ish avatar placeholder with fallback color."""
        pass

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

    class MessageEmbed(AsyncImage, ButtonBehavior):
        """Clickable image embed - click to expand to full screen, click again to minimize.

        Renders at a fixed uniform height (STANDARD_HEIGHT) and scales to fit
        without stretching (allow_stretch=False, keep_ratio=True).
        """
        STANDARD_HEIGHT = dp(200)
        is_expanded = BooleanProperty(False)

        def on_release(self):
            app = App.get_running_app()
            if not app:
                return True
            if self.is_expanded:
                app.dismiss_fullscreen_image(self)
            else:
                app.show_fullscreen_image(self.source, embed=self)
                self.is_expanded = True
            return True


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
        return actions


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




class ChannelButton(ContextMenuMixin, ButtonBehavior, BoxLayout):
    channel_name = StringProperty("")
    badge = NumericProperty(0)

    def build_context_menu(self):
        app = App.get_running_app()
        name = self.channel_name
        cid = app._channels.get(name) if app else None
        actions = [("open #" + name, True,
                    lambda: app and app._switch_channel(name))]
        if cid:
            actions.append(("copy channel id", True,
                            lambda: app and app._copy_text(cid)))
            muted = cid in app._muted_channels
            actions.append(("unmute channel" if muted else "mute channel", True,
                            lambda: app and app._toggle_mute_channel(name, cid)))
            if app and app.i_am_moderator:
                actions.append(("delete channel", True,
                                lambda: app and app._delete_channel(name, cid)))
        return actions


if KIVY:
    class SitePage(BoxLayout):
        """One rendered micron page (site channels). bg_color paints the page
        canvas; the blocks are added as children by _render_site_page."""
        bg_color = ListProperty([0, 0, 0, 0])

        def __init__(self, **kw):
            super().__init__(**kw)
            self.orientation = "vertical"
            self.size_hint_y = None
            self.spacing = dp(4)
            self.padding = [dp(8), dp(8), dp(8), dp(8)]
            self.bind(minimum_height=lambda w, h: setattr(w, "height", h))
            with self.canvas.before:
                self._bg_instr = Color(rgba=self.bg_color)
                self._bg_rect = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._update_bg, size=self._update_bg,
                      bg_color=self._update_bg)

        def _update_bg(self, *a):
            self._bg_instr.rgba = self.bg_color
            self._bg_rect.pos = self.pos
            self._bg_rect.size = self.size

    class MicronSep(Widget):
        """A thin micron separator line."""

        def __init__(self, color=None, **kw):
            super().__init__(**kw)
            self.size_hint_y = None
            self.height = dp(2)
            app = App.get_running_app()
            if color is None:
                color = app.theme_colors.get(
                    'text_dim', (0.35, 0.37, 0.42, 1)) if app else (0.35, 0.37, 0.42, 1)
            with self.canvas:
                Color(*color)
                self._rect = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._up, size=self._up)

        def _up(self, *a):
            self._rect.pos = self.pos
            self._rect.size = self.size

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

    class TermPage(BoxLayout):
        """Guild console view for term channels: one scrolling output label.
        Output arrives as term_data stream events (server-sanitised text);
        input goes through the main input bar -> /api/v1/term/data."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.orientation = "vertical"
            self.size_hint_y = None
            self.padding = [dp(8), dp(8), dp(8), dp(8)]
            self.bind(minimum_height=lambda w, h: setattr(w, "height", h))
            app = App.get_running_app()
            term_text_color = app.theme_colors.get(
                'text_muted', (0.85, 0.88, 0.9, 1)) if app else (0.85, 0.88, 0.9, 1)
            self.output = Label(markup=False, halign="left",
                                color=term_text_color,
                                font_size=sp(13))
            self.output.size_hint_y = None
            self.output.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            self.output.bind(texture_size=lambda inst, ts: setattr(
                inst, "height", max(dp(20), ts[1] if ts else 0)))
            self.add_widget(self.output)

        def append(self, text):
            """Append output; keep the buffer bounded (last ~8000 chars)."""
            buf = (self.output.text or "") + text
            if len(buf) > 8000:
                buf = buf[-8000:]
            self.output.text = buf

        def clear(self):
            self.output.text = ""


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
        left_open = BooleanProperty(True)
        right_open = BooleanProperty(True)
        _current_channel = StringProperty("general")

        def toggle_left(self):
            self.left_open = not self.left_open

        def toggle_right(self):
            self.right_open = not self.right_open

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
                row = BoxLayout(size_hint_y=None, height=dp(44),
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
            for key in THEME_KEYS:
                fld = self._color_fields.get(key)
                if fld is None:
                    continue
                try:
                    app.theme_colors[key] = hex_to_rgba(fld.text)
                except Exception as e:
                    if status:
                        status.text = "Invalid color for %s: %s" % (key, e)
                    return
            app._save_ui_config()
            # Reassign the whole property so KV bindings re-evaluate every
            # app.theme_colors['key'] expression (item mutation alone does not
            # dispatch the property change).
            app.theme_colors = dict(app.theme_colors)
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


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class sHadesClientApp(MDApp):
    title = "sHades"

    guild_api = ObjectProperty(None, allownone=True)
    reticulum = ObjectProperty(None, allownone=True)
    link = ObjectProperty(None, allownone=True)
    # Unified UI palette: edited in the config screen, persisted to
    # ui_config.json, referenced by kv_md.kv as app.theme_colors['key'].
    theme_colors = DictProperty({})

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
        self.my_hash = None
        # Which guild we are pointed at. These are plain attributes (not
        # properties), so they MUST be initialised here: on a fresh install
        # no session has been loaded yet and any read of them before the
        # first connect would raise AttributeError. Seeded with the default
        # community hub so a fresh install already has a valid target.
        self.hub_address = DEFAULT_HUB_ADDRESS
        self.hub_host = DEFAULT_HUB_HOST
        self.hub_port = DEFAULT_HUB_PORT
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._seq = 0
        self._last_ts = 0.0
        self._channels = OrderedDict()   # cid -> name
        self._members = {}               # member_hash -> username
        self._msg_widgets = {}           # msg_id -> MessageRow
        self._popup = None
        self._poll_error_shown = False
        self._ui_populated = False
        # Presence / ping: re-register (ping) every ANNOUNCE_INTERVAL
        # seconds; the guild drops a node that misses two consecutive pings.
        self._announce_stop = threading.Event()
        self._announce_thread = None
        self.ANNOUNCE_INTERVAL = 4.0
        self.announce_host = None
        self.announce_port = None
        self.identity_hash = None
        # v1 stream: long-polls ONLY the visible channel. _stream_cid is the
        # channel the guild should stream to us; setting it (with a None
        # cursor) makes the stream loop resubscribe — one subscription per
        # link is a server-side rule, so switching channels reroutes the flow.
        self._stream_stop = threading.Event()
        self._stream_thread = None
        self._stream_cid = None
        self._stream_cursor = None
        # site channels: kind per cid, the rendered page stack, the page
        # currently shown, and the active theme from the manifest
        self._channel_kinds = {}     # cid -> chat|site|term
        self._site_stack = []        # [(cid, path)] back stack
        self._site_current = None    # (cid, path)
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

        # Presence: mirror the guild's active-user list every PRESENCE_INTERVAL.
        # _active_users maps user_hash -> {"username", "node_hash"} and is
        # rebuilt from each /api/presence response, so a member who stops
        # pinging drops out of the member list instead of lingering in it.
        self._presence_stop = threading.Event()
        self._presence_thread = None
        self.PRESENCE_INTERVAL = 5.0
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
        self._scroll_bound = False

        # Banner images - random selection from media folder on startup.
        self.banner_config_path = None
        self.banner_channel_path = None
        self._select_random_banners()

        # Avatar pfp paths (set by _save_pfp_crop)
        self.my_avatar_orig = None
        self.my_avatar_small = None

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

    def _select_random_banners(self):
        """Pick random banner images from media/ folder on startup.
        One for the connect screen, one for the channel header."""
        media_dir = os.path.join(_app_dir(), "media")
        if not os.path.isdir(media_dir):
            return
        images = [
            f for f in os.listdir(media_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            and f != "1793645765.png"   # client icon/logo — not a banner
        ]
        if not images:
            return
        if len(images) == 1:
            self.banner_config_path = os.path.join(media_dir, images[0])
            self.banner_channel_path = os.path.join(media_dir, images[0])
        else:
            chosen = random.sample(images, 2)
            self.banner_config_path = os.path.join(media_dir, chosen[0])
            self.banner_channel_path = os.path.join(media_dir, chosen[1])
            print("Banner images: config=%s channel=%s" % (
                os.path.basename(self.banner_config_path),
                os.path.basename(self.banner_channel_path),
            ))

    # -- UI theme / config ----------------------------------------------

    def _ui_config_path(self):
        return os.path.join(_app_data_dir(), "ui_config.json")

    def _load_ui_config(self):
        """Load persisted user theme colors from ui_config.json, if any."""
        try:
            with open(self._ui_config_path()) as f:
                saved = json.load(f)
        except Exception:
            return
        if not isinstance(saved, dict):
            return
        for key, value in list(self.theme_colors.items()):
            if key in saved and isinstance(saved[key], str):
                try:
                    self.theme_colors[key] = hex_to_rgba(saved[key])
                except Exception:
                    pass

    def _save_ui_config(self):
        """Persist the current theme colors to ui_config.json."""
        try:
            os.makedirs(_app_data_dir(), exist_ok=True)
            data = {key: rgba_to_hex(value)
                    for key, value in self.theme_colors.items()}
            with open(self._ui_config_path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("save ui config failed:", repr(e))

    def open_config(self):
        """Open the config screen (theme colors)."""
        self.root.current = "config"


    def _channel_cid(self, name):
        """Convert a channel name to its RNS destination hash ID,
        matching the guild's ensure_channel() formula."""
        return hexstr(RNS.Identity.full_hash(b"main_guild:" + name.encode()))

    # -- lifecycle -------------------------------------------------------
    def build(self):
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
        Builder.load_file(os.path.join(_app_dir(), "kv_md.kv"))
        sm = ScreenManager()
        sm.add_widget(ConnectScreen(name="connect"))
        sm.add_widget(MainScreen(name="main"))
        sm.add_widget(ProfileScreen(name="profile"))
        sm.add_widget(ConfigScreen(name="config"))
        # Plain Enter sends the message; Shift+Enter inserts a newline
        # (multiline input). Handled via _on_key_down below.
        Window.bind(on_key_down=self._on_key_down)
        if BOOT_DEBUG:
            _boot_mark("screens ok: %d registered" % len(sm.screens))
            _boot_install(sm.get_screen(sm.current))
        return sm

    def on_start(self):
        if BOOT_DEBUG:
            _boot_mark("on_start")
        # Try to resume a saved session
        sess = self._load_session()
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
                Clock.schedule_once(
                    lambda dt: _boot_reattach(self.root.current), 0)
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0.5)
            # Re-render persisted history (embeds survive close/re-open) once
            # the guild API is up; server-stored attachments re-fetch with a
            # loading placeholder until their bytes arrive.
            Clock.schedule_once(self._load_history_ui, 2.0)
        else:
            # Fresh install: connect to the default community hub without
            # making the user type a server address. The connect screen stays
            # up (showing progress) and is used as the fallback if the
            # default hub is unreachable, so a fresh install is never locked
            # out of the app.
            if BOOT_DEBUG:
                _boot_mark("fresh install -> default hub")
            self._auto_connect_default_hub()

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
            self._refresh_channels_ui()
            self._refresh_server_sidebar()
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
        try:
            os.makedirs(_app_data_dir(), exist_ok=True)
            tmp = self._servers_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._servers, f, indent=2)
            os.replace(tmp, self._servers_path)
        except Exception as e:
            print("save servers failed:", repr(e))

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

    def _attachments_dir(self):
        d = os.path.join(_app_data_dir(), "attachments")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_attachment_blob(self, blob, name):
        """Persist an ephemeral attachment blob to disk; return its local path."""
        d = self._attachments_dir()
        safe = "".join(c for c in name if c.isalnum() or c in "._-") or "attachment"
        fname = "%s_%s" % (hashlib.sha256(blob).hexdigest()[:12], safe)
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, "wb") as f:
                f.write(blob)
        except Exception:
            return None
        return fpath

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
        hub_address = (hub_address or "").strip()
        username = (username or "user").strip() or "user"
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
            self._set_status("Hub address required (ip:port)")
            return

        # Parse ip:port
        if ":" not in hub_address:
            self._set_status("Hub address must be ip:port (e.g. 192.168.1.50:29221)")
            return
        host, port_str = hub_address.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            self._set_status("Invalid port number")
            return

        # Validate hashes if provided
        if node_hash and not re.fullmatch(r"[0-9a-f]{32}", node_hash):
            self._set_status("Invalid node_hash (need 32 hex)")
            return
        if delivery_hash and not re.fullmatch(r"[0-9a-f]{32}", delivery_hash):
            self._set_status("Invalid delivery_hash (need 32 hex)")
            return

        # Add/update server in the list
        self._add_server(hub_address, username, node_hash, delivery_hash)

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
        # RNS.Reticulum() installs signal handlers and must run on the main
        # thread. Schedule it there, then do blocking link work in a bg thread.
        # If RNS is already initialized (retry), skip initialization.
        if self.reticulum is not None:
            Clock.schedule_once(lambda dt: self._connect_after_rns(), 0)
        else:
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0)

    # -- server sidebar ------------------------------------------------------
    def _refresh_server_sidebar(self):
        """Populate the server grid with server buttons."""
        try:
            main = self.root.get_screen("main")
            grid = main.ids.server_grid
        except Exception:
            return
        grid.clear_widgets()
        for idx, s in enumerate(self._servers):
            display_name = s.get("display_name", s.get("host", "Server"))
            btn = Button(
                text=display_name[:12],  # truncate long names
                size_hint_y=None,
                height=scaled(dp(48)),
                font_size=sp(10),
                background_color=(
                    self.theme_colors['primary']
                    if idx == self._active_server_idx
                    else self.theme_colors['sidebar_ctrl']),
                color=(
                    self.theme_colors['btn_text']
                    if idx == self._active_server_idx
                    else self.theme_colors['text']),
                halign='center',
                valign='middle',
            )
            btn.bind(on_press=lambda inst, i=idx: self._select_server(i))
            btn.bind(size=lambda inst, val: setattr(inst, 'text_size', (inst.width, None)))
            grid.add_widget(btn)

    def _select_server(self, idx):
        """Switch to a different server."""
        if idx == self._active_server_idx:
            return
        self._set_active_server(idx)
        self._refresh_server_sidebar()
        # Clear all channel/chat state for the new server
        self._channels.clear()
        self._channel_kinds.clear()
        self._channel_unread.clear()
        self._msg_widgets.clear()
        self._older.clear()
        self._seq = 0
        self._last_ts = 0.0
        self._stream_cid = None
        self._stream_cursor = None
        self._site_stack.clear()
        self._site_current = None
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
        # Reconnect to the new server
        if self.reticulum is not None:
            Clock.schedule_once(lambda dt: self._connect_after_rns(), 0)
        else:
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0)
        self._set_status("Switching to %s..." % self._servers[idx].get("display_name", "server"))

    def _open_add_server_popup(self):
        """Popup to add a new server (guild)."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.metrics import dp, sp

        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))
        box.add_widget(Label(text="Add Server", font_size=sp(18), size_hint_y=None,
                       height=dp(30), color=self.theme_colors['text']))

        hub_in = TextInput(hint_text="Hub address (ip:port or shades://...)",
                           multiline=False, size_hint_y=None, height=dp(40),
                           background_color=self.theme_colors['input_bg'],
                           foreground_color=self.theme_colors['text'],
                           cursor_color=self.theme_colors['text'])
        box.add_widget(hub_in)

        user_in = TextInput(hint_text="Username",
                            multiline=False, size_hint_y=None, height=dp(40),
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        user_in.text = self.username
        box.add_widget(user_in)

        node_in = TextInput(hint_text="Node hash (32 hex, optional)",
                            multiline=False, size_hint_y=None, height=dp(40),
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        box.add_widget(node_in)

        delivery_in = TextInput(hint_text="Delivery hash (32 hex, optional)",
                                multiline=False, size_hint_y=None, height=dp(40),
                                background_color=self.theme_colors['input_bg'],
                                foreground_color=self.theme_colors['text'],
                                cursor_color=self.theme_colors['text'])
        box.add_widget(delivery_in)

        name_in = TextInput(hint_text="Display name (optional)",
                            multiline=False, size_hint_y=None, height=dp(40),
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        box.add_widget(name_in)

        status = Label(text="", size_hint_y=None, height=dp(
            20), color=self.theme_colors['accent'], font_size=sp(11))
        box.add_widget(status)

        btn_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))

        def do_add(*_):
            hub = (hub_in.text or "").strip()
            user = (user_in.text or "").strip() or "user"
            node = (node_in.text or "").strip().lower()
            delivery = (delivery_in.text or "").strip().lower()
            name = (name_in.text or "").strip() or None
            if not hub:
                status.text = "Hub address required"
                return
            if ":" not in hub and not hub.startswith("shades://"):
                status.text = "Hub must be ip:port or shades://..."
                return
            if node and not re.fullmatch(r"[0-9a-f]{32}", node):
                status.text = "Invalid node hash"
                return
            if delivery and not re.fullmatch(r"[0-9a-f]{32}", delivery):
                status.text = "Invalid delivery hash"
                return
            self._add_server(hub, user, node, delivery, name)
            self._refresh_server_sidebar()
            popup.dismiss()
            # Connect to the new server
            self.connect_to_guild(hub, user, node, delivery)

        add_btn = Button(
            text="Add & Connect",
            size_hint_y=None,
            height=scaled(dp(44)),
            pos_hint={"center_y": 0.5},
            background_color=self.theme_colors['primary'],
            color=self.theme_colors['btn_text'])
        add_btn.bind(on_press=do_add)
        cancel_btn = Button(
            text="Cancel",
            size_hint_y=None,
            height=scaled(dp(44)),
            pos_hint={"center_y": 0.5},
            background_color=self.theme_colors['sidebar_ctrl'],
            color=self.theme_colors['text'])
        cancel_btn.bind(on_press=lambda *_: popup.dismiss())
        btn_row.add_widget(add_btn)
        btn_row.add_widget(cancel_btn)
        box.add_widget(btn_row)

        popup = Popup(title="", content=box, size_hint=(0.8, 0.75), auto_dismiss=False)
        popup.open()

    def _init_reticulum(self):
        """Main thread only: create RNS instance (signal handlers), then
        spawn a background thread for the blocking link establishment."""
        if BOOT_DEBUG:
            _boot_mark("RNS: writing config")
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            os.makedirs(storage, exist_ok=True)

            # Write an RNS config that routes to the guild. If a hub address was
            # provided, use a TCPClientInterface to reach it directly;
            # otherwise use AutoInterface for LAN multicast discovery.
            self.client_port = 29222
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

            reticulum = RNS.Reticulum(configdir=storage)
            self.reticulum = reticulum
            if BOOT_DEBUG:
                _boot_mark("RNS: instance up (config ok)")
        except Exception as e:
            print("init reticulum error:", repr(e))
            self._set_status("connect failed: %s" % e)
            return
        # Identity and the LXMRouter must be built on the MAIN thread --
        # see _init_identity_and_lxmf. Only the blocking link work below
        # moves to a worker.
        self._init_identity_and_lxmf()
        if BOOT_DEBUG:
            _boot_mark("identity + LXMF router ok, link worker starting")
        threading.Thread(target=self._connect_after_rns, daemon=True).start()

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
                return ("target_host = %s" % self.hub_host) not in content or \
                       ("target_port = %d" % int(self.hub_port)) not in content
            return "AutoInterface" not in content
        except Exception:
            return True

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
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            ipath = os.path.join(storage, "identity")
            if os.path.exists(ipath):
                self.client_identity = RNS.Identity.from_file(ipath)
                if self.client_identity is None:
                    # Corrupted or empty identity file — create a fresh one
                    self.client_identity = RNS.Identity()
                    self.client_identity.to_file(ipath)
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
            try:
                import LXMF
                self.lxmf_router = LXMF.LXMRouter(
                    identity=self.client_identity,
                    storagepath=os.path.join(_app_data_dir(), "lxmf"))
                self.lxmf_delivery = self.lxmf_router.register_delivery_identity(
                    self.client_identity,
                    display_name=(self.username or "sHades"))
                self.lxmf_router.register_delivery_callback(self._on_dm_delivery)
                # announce now so friends can hear our lxmf.delivery
                self.lxmf_router.announce(self.lxmf_delivery.hash)
            except Exception as e:
                print("LXMF router init failed (DMs disabled):", repr(e))
                self.lxmf_router = None
                self.lxmf_delivery = None
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
                self.guild_node_hash = self._discover_node_hash(reticulum)
                if not self.guild_node_hash:
                    raise RuntimeError(
                        "Could not discover guild node_hash from hub announces. "
                        "Ask the hub admin for its node_hash and try again.")
                print(f"Discovered guild node_hash via announce: {self.guild_node_hash}")

            # Validate that RNS knows this identity (from hearing its announce)
            self.node_hash = self.guild_node_hash
            nh = bytes.fromhex(self.guild_node_hash)
            nid = RNS.Identity.recall(nh)
            if nid is None:
                raise RuntimeError(
                    "RNS does not know identity for node_hash %s — "
                    "the guild's announce was not heard yet. Try again." % self.guild_node_hash)
            print(f"RNS recalled identity for node_hash {self.guild_node_hash}")

            # link to guild node — retry up to 3 times
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
            self.link = link

            self.guild_api = GuildAPI(reticulum, link)
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

            # Register with guild so others can find me via ip:port, then
            # re-register every ANNOUNCE_INTERVAL seconds — each re-register
            # is a ping; the guild drops a node that misses two consecutive
            # pings (8 s) from its active user list.
            try:
                import socket
                my_host = socket.gethostbyname(socket.gethostname())
                self.announce_host = my_host
                self.announce_port = self.client_port
                self.identity_hash = hexstr(self.client_identity.hash)
                self.guild_api.register_node(
                    self.node_hash, my_host, self.client_port,
                    self.identity_hash)
                print(f"Registered with guild: {my_host}:{self.client_port}")
                self._start_announce()
                self._start_presence()
            except Exception as e:
                print(f"Node registration failed: {e}")

            Clock.schedule_once(lambda dt: self._switch_main(), 0)
            Clock.schedule_once(lambda dt: self._start_poll(), 0)
            Clock.schedule_once(lambda dt: self._start_streamer(), 0)
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

    def _start_poll(self):
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        tick = 0
        while not self._poll_stop.is_set():
            tick += 1
            try:
                # periodic channel-index refresh: renames, topics, new
                # channels and unread badges stay current (cheap JSON call)
                if tick % 5 == 0:
                    self._refresh_channels_from_guild()
                main = self.root.get_screen("main")
                cid = getattr(main, "_current_channel", "general")
                st = self.guild_api.state(self._channel_cid(cid))
                if st.get("ok"):
                    self._poll_error_shown = False
                    seq = st.get("seq", 0)
                    last_ts = st.get("last_ts", 0.0)
                    msg_count = len(st.get("messages", []))
                    print("poll state ok: seq=%s last_ts=%s msgs=%s" % (seq, last_ts, msg_count))
                    if seq != self._seq or last_ts != self._last_ts:
                        self._seq = seq
                        self._last_ts = last_ts
                        self._fetch_messages(cid)
                        self._refresh_members_ui(self._channel_cid(cid))
                    else:
                        # no change, but make sure UI is populated on first poll
                        if not getattr(self, "_ui_populated", False):
                            self._ui_populated = True
                            self._fetch_messages(cid)
                            self._refresh_members_ui(self._channel_cid(cid))
                else:
                    err = st.get("error", "unknown")
                    self._poll_error_shown = True
                    Clock.schedule_once(
                        lambda dt: self._set_status("poll failed: %s" % err), 0)
                    print("poll state failed: %s" % err)
                Clock.schedule_once(lambda dt: self._ensure_general_channel(), 0)
            except Exception as exc:
                print("poll exception: %s" % repr(exc))
                self._poll_error_shown = True
                err_msg = "poll exception: %s" % exc
                Clock.schedule_once(
                    lambda dt: self._set_status(err_msg), 0)
            self._poll_stop.wait(3.0)

    def _start_streamer(self):
        """Long-poll stream for the visible channel only (JSON API v1)."""
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(target=self._stream_loop,
                                               daemon=True)
        self._stream_thread.start()

    def _stream_loop(self):
        """Subscribe to the active channel and long-poll it.

        Only ONE subscription per link exists server-side, so pointing
        _stream_cid at a new channel (cursor=None) reroutes the stream —
        background channels get nothing until they are opened again.
        """
        while not self._stream_stop.is_set():
            if not self.guild_api or not self._stream_cid:
                self._stream_stop.wait(1.0)
                continue
            try:
                if self._stream_cursor is None:
                    res = self.guild_api.stream_subscribe(self._stream_cid)
                    if res.get("ok"):
                        self._stream_cursor = res.get("seq", 0)
                    else:
                        self._stream_stop.wait(2.0)
                    continue
                res = self.guild_api.stream_poll(self._stream_cursor, timeout_s=4.0)
                if not res.get("ok"):
                    if res.get("error") == "not subscribed":
                        self._stream_cursor = None
                    self._stream_stop.wait(2.0)
                    continue
                if res.get("reset"):
                    # replay window rotated past our cursor: resync
                    self._stream_cursor = None
                    self._resync_stream_channel()
                    continue
                events = res.get("events") or []
                if events:
                    self._stream_cursor = res.get("cursor", self._stream_cursor)
                    self._apply_stream_events(events)
            except Exception as e:
                print("stream exception:", repr(e))
                self._stream_stop.wait(2.0)

    def _resync_stream_channel(self):
        """Fetch the subscribed channel's history after a gap/reset."""
        try:
            main = self.root.get_screen("main")
            name = getattr(main, "_current_channel", "general")
            if self._channel_cid(name) == self._stream_cid:
                self._fetch_messages(name)
        except Exception:
            pass

    def _apply_stream_events(self, events):
        """Dispatch stream events. Runs on the stream thread: UI changes go
        through _fetch_messages (which Clock-schedules) or Clock directly."""
        cid = self._stream_cid
        touched = False
        for ev in events:
            kind = ev.get("kind")
            data = ev.get("data") or {}
            if kind == "msg":
                if (data.get("msg") or {}).get("channel_id") == cid:
                    touched = True
            elif kind in ("edit", "del"):
                touched = True
            elif kind == "presence":
                users = {}
                for u in data.get("users") or []:
                    if not isinstance(u, dict):
                        continue
                    uh = (u.get("user_hash") or "").lower()
                    if uh:
                        users[uh] = {"username": u.get("username") or "",
                                     "node_hash": u.get("node_hash")}
                self._active_users = users
                self._presence_ok = True
                Clock.schedule_once(lambda dt: self._refresh_presence_ui(), 0)
            elif kind == "site_patch":
                # a site resource or the manifest changed under us: re-theme
                # and re-render the page we are on
                if isinstance(data.get("manifest"), dict):
                    self._site_theme = data["manifest"].get("theme") or self._site_theme
                if self._site_current:
                    Clock.schedule_once(lambda dt: self._site_load_page(
                        self._site_current[0], self._site_current[1]), 0)
            elif kind == "reaction":
                # update the affected row's chips in place (react does not
                # bump the channel seq, so the reconciler will not refetch)
                def apply_rxn(dt):
                    self._set_row_reactions(data.get("msg_id"),
                                            self._reactions_from_counts(
                                                data.get("msg_id"), data))
                Clock.schedule_once(apply_rxn, 0)
            elif kind == "channel_deleted":
                # a channel (possibly the one we are watching) was deleted
                def apply_del(dt):
                    dead = data.get("channel")
                    self._channel_kinds.pop(dead, None)
                    self._channel_unread.pop(dead, None)
                    self._term_sessions.pop(dead, None)
                    name = data.get("name") or "channel"
                    self._refresh_channels_from_guild()
                    try:
                        main = self.root.get_screen("main")
                        cur = self._channel_cid(
                            getattr(main, "_current_channel", "general"))
                        if cur == dead:
                            self._set_status("#%s was deleted" % name)
                            self._switch_channel("general")
                    except Exception:
                        pass
                Clock.schedule_once(apply_del, 0)
            elif kind == "term_data":
                # console output (server-sanitised); "clear" wipes the buffer
                def apply_term(dt):
                    if not self._term_page:
                        return
                    if data.get("clear"):
                        self._term_page.clear()
                    elif data.get("text"):
                        self._term_page.append(data["text"])
                Clock.schedule_once(apply_term, 0)
            # "typing" events are consumed by a later phase
        if touched:
            # full refresh of the visible channel; _render_message dedupes
            # by msg_id, so events already rendered are skipped
            self._resync_stream_channel()

    def _start_announce(self):
        """Re-register our node every ANNOUNCE_INTERVAL seconds.

        Each re-register is a ping; the guild drops a node that misses two
        consecutive pings (8 s) from its active user list. Safe to call more
        than once: an existing thread is stopped before a new one starts.
        """
        if self._announce_thread is not None and self._announce_thread.is_alive():
            self._announce_stop.set()
        self._announce_stop.clear()
        self._announce_thread = threading.Thread(target=self._announce_loop,
                                                 daemon=True)
        self._announce_thread.start()

    def _announce_loop(self):
        while not self._announce_stop.wait(self.ANNOUNCE_INTERVAL):
            if not self.guild_api or not self.node_hash:
                continue
            try:
                self.guild_api.register_node(
                    self.node_hash, self.announce_host,
                    self.announce_port, self.identity_hash)
            except Exception as e:
                print("announce ping failed:", repr(e))

    def _start_presence(self):
        """Fetch active node list from guild every PRESENCE_INTERVAL seconds."""
        if self._presence_thread is not None and self._presence_thread.is_alive():
            self._presence_stop.set()
        self._presence_stop.clear()
        self._presence_thread = threading.Thread(target=self._presence_loop,
                                                 daemon=True)
        self._presence_thread.start()

    def _presence_loop(self):
        while not self._presence_stop.wait(self.PRESENCE_INTERVAL):
            if not self.guild_api:
                continue
            try:
                res = self.guild_api.presence()
                if not res.get("ok"):
                    continue
                users = {}
                for u in res.get("users") or []:
                    if not isinstance(u, dict):
                        continue
                    uh = (u.get("user_hash") or "").lower()
                    if uh:
                        users[uh] = {"username": u.get("username") or "",
                                     "node_hash": u.get("node_hash")}
                self._active_users = users
                self._presence_ok = True
                Clock.schedule_once(lambda dt: self._refresh_presence_ui(), 0)
            except Exception as e:
                print("presence fetch failed:", repr(e))

    def _refresh_presence_ui(self):
        """Rebuild the member sidebar from the presence list.

        The list is *rebuilt*, never just recoloured: members come straight from
        the guild's active-user list, so a client that stops pinging is dropped
        by the guild's reaper and then disappears from the sidebar on the next
        presence tick. Nothing accumulates.
        """
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        self._refresh_members_ui_main(getattr(main, "_current_channel", "general"))

    def _fetch_messages(self, channel, since_id=None, since_ts=None,
                        before=None, limit=50, prepend=False):
        # site/term channels don't have a message ledger — their grid is a
        # rendered micron page; never stomp it with chat fetches
        if self._channel_kinds.get(self._channel_cid(channel), "chat") != "chat":
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
                return
            msgs = res.get("messages", []) or []
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
                        for m in msgs:
                            self._render_message(grid, m)
                        Clock.schedule_once(
                            lambda dt: self._scroll_to_bottom(), 0.1)
                    for u in updates:
                        self._apply_update(u)
                except Exception as e:
                    print("render messages error:", e)
            Clock.schedule_once(apply, 0)
        threading.Thread(target=work, daemon=True).start()

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

    def _render_message(self, grid, m):
        msg_id = m.get("msg_id")
        if msg_id in self._msg_widgets:
            return
        author = m.get("author_hash", "")
        uname = m.get("username") or ("user-" + author[:6])
        self._record_member(author, uname)
        content = m.get("content") or ""
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
            content_markup=md_to_kivy(content),
            avatar_source=avatar_source,
            msg_id=msg_id,
            is_own=is_own,
        )
        self._msg_widgets[msg_id] = row
        grid.add_widget(row)

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

    # -- reactions (phase 6) ----------------------------------------------
    REACTION_PRESETS = ("+1", "-1", "laugh", "heart", "eyes", "question")

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
                         height=dp(20), spacing=dp(6))
        for e in reactions:
            if not isinstance(e, dict) or not e.get("emoji"):
                continue
            chip = ClickableLabel(text=":%s %d" % (e["emoji"], e.get("count", 0)),
                                  color=(0.75, 0.78, 0.85, 1), font_size=sp(12))
            chip.size_hint_x = None
            chip.width = dp(24 + 9 * len(e["emoji"]))
            chip.bind(on_press=lambda inst, m=msg_id, em=e["emoji"]:
                      self._toggle_reaction(m, em))
            line.add_widget(chip)
        plus = ClickableLabel(text="[+]", color=(0.5, 0.53, 0.6, 1), font_size=sp(12))
        plus.size_hint_x = None
        plus.width = dp(30)
        plus.bind(on_press=lambda inst, m=msg_id: self._open_reaction_popup(m))
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

    def _open_reaction_popup(self, msg_id):
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        line = BoxLayout(orientation="horizontal", spacing=dp(6), size_hint_y=None,
                         height=dp(40))
        for emoji in self.REACTION_PRESETS:
            btn = Button(text=":%s" % emoji, size_hint_x=None, width=dp(70),
                         size_hint_y=None, height=scaled(dp(40)),
                         pos_hint={"center_y": 0.5})
            btn.bind(on_press=lambda inst, m=msg_id, em=emoji:
                     (self._toggle_reaction(m, em), popup.dismiss()))
            line.add_widget(btn)
        box.add_widget(line)
        popup = Popup(title="react", content=box, size_hint=(0.7, 0.25))
        self._popup = popup
        popup.open()

    # -- search (phase 6) --------------------------------------------------
    def open_search(self, prefill=""):
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        query = TextInput(text=prefill or "", multiline=False, size_hint_y=None,
                          height=dp(40),
                          hint_text="search messages (min 2 chars)")
        results = Label(markup=True, halign="left", valign="top",
                        color=(0.85, 0.88, 0.9, 1), font_size=sp(13))
        results.size_hint_y = 1
        results.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))

        def run_search(*a):
            q = (query.text or "").strip()
            if len(q) < 2:
                return

            def work():
                try:
                    res = self.guild_api.search(q)
                except Exception as e:
                    res = {"ok": 0, "error": repr(e)}

                def apply(dt):
                    if not res.get("ok"):
                        results.text = "[i]search failed: %s[/i]" % res.get("error", "")
                        return
                    rows = res.get("results") or []
                    if not rows:
                        results.text = "[i]no matches[/i]"
                        return
                    out = []
                    for r in rows[:20]:
                        ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
                        snippet = (r.get("content") or "")[:80].replace("\n", " ")
                        out.append("[b]%s[/b]  %s\n  %s" % (r.get("username", "?"), ts, snippet))
                    results.text = "\n".join(out)
                Clock.schedule_once(apply, 0)
            threading.Thread(target=work, daemon=True).start()
        query.bind(on_text_validate=run_search)
        btn = Button(text="search", size_hint_y=None, height=scaled(dp(36)))
        btn.bind(on_press=run_search)
        box.add_widget(query)
        box.add_widget(btn)
        box.add_widget(results)
        popup = Popup(title="search", content=box, size_hint=(0.85, 0.6))
        self._popup = popup
        popup.open()

    def _add_attachment_embed(self, row, name, blob):
        """Render one attachment: images inline, everything else as a label."""
        print(
            f"[TRACE] _add_attachment_embed: name={name} "
            f"blob_size={len(blob)} ext={os.path.splitext(name)[1].lower()}",
            flush=True)
        dlog(
            f"_add_attachment_embed: name={name} "
            f"blob_size={len(blob)} ext={os.path.splitext(name)[1].lower()}")
        ext = os.path.splitext(name)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(blob)
            tmp.close()
            emb = MessageEmbed(source=tmp.name, size_hint_y=None, height=dp(200))
            row.ids.embed_container.add_widget(emb)
        elif ext in (".mp3", ".ogg", ".wav", ".flac"):
            row.ids.embed_container.add_widget(
                Label(
                    text="[audio] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )
        elif ext in (".mp4", ".webm"):
            row.ids.embed_container.add_widget(
                Label(
                    text="[video] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )
        else:
            row.ids.embed_container.add_widget(
                Label(
                    text="[file] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )

    def _fetch_attachment(self, row, msg_id, att):
        """Download an attachment blob from the guild and render it.

        The guild only returns metadata; the bytes are served by its
        /file/attachments/... handler. Download off the main thread and add
        the embed back on the UI thread. A loading placeholder is shown until
        the image has been received.
        """
        print(
            f"[TRACE] _fetch_attachment: msg_id={msg_id} "
            f"name={att.get('name')!r} path={att.get('path')!r}", flush=True)
        dlog(
            f"_fetch_attachment: msg_id={msg_id} name={att.get('name')!r} path={att.get('path')!r}")
        name = att.get("name") or ""
        path = att.get("path") or ""
        if not path:
            print("[TRACE] _fetch_attachment: no path, skipping", flush=True)
            dlog("_fetch_attachment: no path, skipping")
            return

        self._add_loading_placeholder(row)

        def work():
            print(f"[TRACE] _fetch_attachment.work: fetching {path}", flush=True)
            dlog(f"_fetch_attachment.work: fetching {path}")
            try:
                blob = self.guild_api.fetch_file(path)
            except Exception:
                blob = None
            if not blob:
                print(
                    f"[TRACE] _fetch_attachment.work: fetch returned empty for {path}", flush=True)
                dlog(f"_fetch_attachment.work: fetch returned empty for {path}")
                Clock.schedule_once(
                    lambda dt: self._remove_placeholder(row), 0)
                return
            print(
                f"[TRACE] _fetch_attachment.work: received {len(blob)} bytes "
                f"for {path}", flush=True)
            dlog(f"_fetch_attachment.work: received {len(blob)} bytes for {path}")

            def apply(dt):
                try:
                    self._remove_placeholder(row)
                    # Always add the embed to the row's container. The row may
                    # have been removed from the grid (channel switch) while the
                    # download was in flight, but the embed container still
                    # exists on the row object and the embed will show when the
                    # row is re-added to the grid.
                    main = self.root.get_screen("main")
                    if main is None:
                        return
                    if msg_id and msg_id not in self._msg_widgets:
                        # Row was removed from widgets; re-add it to the grid
                        # so the embed is visible.
                        grid = main.ids.message_grid
                        if grid is not None and row.parent is None:
                            grid.add_widget(row)
                    print(f"[TRACE] _fetch_attachment.apply: adding embed for {name}", flush=True)
                    dlog(f"_fetch_attachment.apply: adding embed for {name}")
                    self._add_attachment_embed(row, name, blob)
                except Exception as e:
                    print("attachment render error:", e)

            Clock.schedule_once(apply, 0)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_avatar(self, key):
        # re-render rows that use this avatar
        path = IMAGE_CACHE.get(key)
        if not path:
            return
        author = key[3:]  # strip "av:" prefix
        for row in self._msg_widgets.values():
            if row.author_hash == author:
                row.avatar_source = ""   # force Kivy to re-bind source
                row.avatar_source = path

    def _apply_update(self, u):
        # edit / delete delta
        msg_id = u.get("msg_id")
        content = u.get("content", "")
        deleted = bool(u.get("deleted_at"))
        # find widget
        row = self._msg_widgets.get(msg_id)
        if not row:
            return
        Clock.schedule_once(lambda dt: self._do_update(row, content, deleted), 0)

    def _do_update(self, row, content, deleted):
        if deleted:
            try:
                grid = self.root.get_screen("main").ids.message_grid
                grid.remove_widget(row)
            except Exception:
                pass
            self._msg_widgets.pop(row.msg_id, None)
        else:
            row.content = content
            row.content_markup = md_to_kivy(content)
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
    def _ensure_general_channel(self):
        # only add once
        if not hasattr(self, "_channels_added"):
            self._channels_added = True
            for name in ("general", "announcements", "random"):
                self._add_channel(name)

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
                    if name not in self._channels:
                        self._channels[name] = cid
                        self._add_channel(name)
            self._channel_unread.update(
                {c["id"]: c.get("unread", 0) for c in chans
                 if isinstance(c, dict) and c.get("id")})
            Clock.schedule_once(lambda dt: self._update_channel_badges(), 0)
            Clock.schedule_once(apply, 0)
        threading.Thread(target=work, daemon=True).start()

    def _channel_kind(self, cid):
        return self._channel_kinds.get(cid, "chat")

    def _fetch_whoami(self):
        """Fetch our own identity/role view (gates moderator menu items)."""
        if not self.guild_api:
            return

        def work():
            try:
                res = self.guild_api.whoami()
            except Exception as e:
                print("whoami failed:", repr(e))
                return
            if res.get("ok"):
                self.i_am_moderator = bool(res.get("is_moderator"))
                self.i_am_admin = bool(res.get("is_admin"))
                self.my_roles = res.get("roles") or []
        threading.Thread(target=work, daemon=True).start()

    def _can_manage_roles(self):
        """UI gate: any of my roles carries MANAGE_ROLES or ADMIN."""
        return any(r.get("perms", 0) & (self.PERM_MANAGE_ROLES |
                                        self.PERM_ADMIN)
                   for r in self.my_roles)

    def _open_roles_popup(self, target_hash, target_name):
        """Role manager (U5): list roles, toggle assignment for one user,
        create a new role. Server enforces MANAGE_ROLES; this popup only
        appears for users who have it."""
        if not self.guild_api or not self._can_manage_roles():
            return
        box = BoxLayout(orientation="vertical", spacing=4, padding=4)
        title = Label(text="roles: @%s" % (target_name or target_hash[:8]),
                      size_hint_y=None, height=22)
        box.add_widget(title)
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, size_hint_y=None, spacing=2)
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        box.add_widget(scroll)
        row = BoxLayout(size_hint_y=None, height=30, spacing=4)
        name_in = TextInput(hint_text="new role name", multiline=False)
        row.add_widget(name_in)
        create_btn = Button(text="create", size_hint_x=None, width=70,
                            size_hint_y=None, height=scaled(30),
                            font_size=sp(14), pos_hint={"center_y": 0.5})
        row.add_widget(create_btn)
        box.add_widget(row)
        close = Button(text="close", size_hint_y=None, height=scaled(28))
        box.add_widget(close)
        popup = Popup(title="roles", content=box, size_hint=(0.7, 0.6))
        self._popup = popup

        # load_gen guards against out-of-order responses: toggling two roles
        # quickly starts two overlapping fetches, and without this the
        # slower one appends its rows on top of the faster one's.
        load_gen = [0]

        def refresh():
            """Reload the role list.

            The guild call blocks on the RNS link for up to the request
            timeout, so it runs on a worker thread; the widget rebuild
            happens back on the main thread.
            """
            grid.clear_widgets()
            load_gen[0] += 1
            gen = load_gen[0]

            def add_role(r, member):
                def toggle(btn):
                    def work():
                        try:
                            if member:
                                res = self.guild_api.roles_unassign(
                                    r["role_id"], target_hash)
                            else:
                                res = self.guild_api.roles_assign(
                                    r["role_id"], target_hash)
                        except Exception as e:
                            # Bind the message to a plain local: Python unbinds
                            # `e` when the except block exits, so a lambda
                            # closing over it would raise NameError later.
                            err = "roles: %r" % e
                            Clock.schedule_once(
                                lambda dt: self._show_notice(err), 0)
                            return
                        if res.get("ok"):
                            Clock.schedule_once(lambda dt: refresh(), 0)
                        else:
                            Clock.schedule_once(
                                lambda dt: self._show_notice(
                                    "roles: %s" % res.get("error", "failed")), 0)
                    threading.Thread(target=work, daemon=True).start()
                mark = "[x] " if member else "[  ] "
                btn = Button(text=mark + r["name"], size_hint_y=None,
                             height=scaled(24), font_size=sp(13),
                             background_color=(0.2, 0.2, 0.25, 1))
                btn.bind(on_press=toggle)
                grid.add_widget(btn)

            def work():
                try:
                    res = self.guild_api.roles_list()
                except Exception as e:
                    err = "roles: %r" % e
                    Clock.schedule_once(
                        lambda dt: self._show_notice(err), 0)
                    return

                def apply(dt):
                    if gen != load_gen[0]:
                        return  # a newer refresh superseded this response
                    if not res.get("ok"):
                        self._show_notice(
                            "roles: %s" % res.get("error", "failed"))
                        return
                    members = set()
                    for r in res.get("roles", []):
                        members = set(r.get("members") or [])
                        add_role(r, target_hash in members)
                Clock.schedule_once(apply, 0)
            threading.Thread(target=work, daemon=True).start()

        def on_create():
            name = name_in.text.strip()
            if not name:
                return

            def work():
                try:
                    res = self.guild_api.roles_create(name)
                except Exception as e:
                    err = "roles: %r" % e
                    Clock.schedule_once(
                        lambda dt: self._show_notice(err), 0)
                    return
                if res.get("ok"):
                    Clock.schedule_once(
                        lambda dt: setattr(name_in, "text", ""), 0)
                    Clock.schedule_once(lambda dt: refresh(), 0)
                else:
                    Clock.schedule_once(
                        lambda dt: self._show_notice(
                            "roles: %s" % res.get("error", "failed")), 0)
            threading.Thread(target=work, daemon=True).start()
        create_btn.bind(on_press=lambda inst: on_create())
        close.bind(on_press=lambda inst: popup.dismiss())
        refresh()
        popup.open()

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
        row = BoxLayout(size_hint_y=None, height=36, spacing=4)
        text_in = TextInput(hint_text="message (direct lxmf)",
                            multiline=False)
        row.add_widget(text_in)
        send = Button(text="send", size_hint_x=None, width=70,
                      size_hint_y=None, height=scaled(36),
                      font_size=sp(14), pos_hint={"center_y": 0.5})
        row.add_widget(send)
        box.add_widget(row)
        close = Button(text="close", size_hint_y=None, height=scaled(28))
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
        popup.open()

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
            grid.add_widget(Label(
                text="%s: %s" % (who, r["text"][:200]),
                size_hint_y=None, height=dp(24),
                color=(0.85, 0.87, 0.9, 1) if r["dir"] == "out"
                else (1, 1, 1, 1),
                font_size=sp(12), halign="left", valign="middle"))
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
                self.lxmf_router.handle_outbound(lxm)
            except Exception as e:
                err = repr(e)
                Clock.schedule_once(lambda dt: self._set_status(
                    "dm send failed: %s" % err), 0)
                return
            try:
                dms.append_dm(dms.dm_history_path(_app_data_dir(), peer_hash),
                              "out", text)
            except ValueError:
                pass
            Clock.schedule_once(lambda dt: self._refresh_dm_popup(), 0)
        threading.Thread(target=work, daemon=True).start()

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
    def _open_admin_panel(self):
        """Server settings panel: config values, moderator whitelist and
        the audit log. Admin-only (the server enforces it regardless —
        this panel just avoids showing admins a broken screen)."""
        if not self.guild_api or not getattr(self, "i_am_admin", False):
            return
        box = BoxLayout(orientation="vertical", spacing=4, padding=6)
        tabs = BoxLayout(size_hint_y=None, height=32, spacing=4)
        box.add_widget(tabs)
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, size_hint_y=None, spacing=3, padding=[2, 2])
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        box.add_widget(scroll)
        close = Button(text="close", size_hint_y=None, height=scaled(28))
        box.add_widget(close)
        popup = Popup(title="server settings", content=box, size_hint=(0.8, 0.8))
        self._admin_popup = popup
        close.bind(on_press=lambda inst: popup.dismiss())

        def set_status(txt):
            Clock.schedule_once(lambda dt: self._set_status(txt), 0)

        # Every tab fetches from the guild. Those calls block on the RNS
        # link for up to the request timeout (10s), so they must NOT run on
        # the main thread — an unresponsive guild would freeze the whole UI.
        # Each loader therefore fetches on a background thread and builds
        # its widgets back on the main thread via Clock.schedule_once.
        #
        # load_gen guards against out-of-order responses: switching tabs
        # quickly would otherwise let a slow earlier response append its
        # rows to a grid the newer response already cleared.
        load_gen = [0]

        def fetch(view_fn, on_ok):
            """Run view_fn() on a worker thread; call on_ok(res) on the
            main thread with the result."""
            gen = load_gen[0]

            def work():
                try:
                    res = view_fn()
                except Exception as e:
                    set_status("%r" % e)
                    return
                if gen != load_gen[0]:
                    return  # a newer tab superseded this response
                Clock.schedule_once(lambda dt: on_ok(res), 0)
            threading.Thread(target=work, daemon=True).start()

        def load_config():
            def render(res):
                if not res.get("ok"):
                    set_status("config: %s" % res.get("error", "failed"))
                    return
                cfg = res.get("config", {})
                for key, val in sorted(cfg.items()):
                    row = BoxLayout(size_hint_y=None, height=dp(32), spacing=4)
                    row.add_widget(Label(text=key, size_hint_x=0.35,
                                         halign="left", valign="middle",
                                         font_size=sp(12)))
                    entry = TextInput(text=str(val), multiline=False,
                                      size_hint_x=0.45)
                    row.add_widget(entry)
                    save = Button(text="save", size_hint_x=None, width=60,
                                  size_hint_y=None, height=scaled(dp(32)),
                                  font_size=sp(11), pos_hint={"center_y": 0.5})
                    row.add_widget(save)

                    def do_save(k=key, e=entry):
                        # Read the widget on the main thread; the worker
                        # must not touch widget state.
                        val = e.text

                        def save_work():
                            try:
                                r = self.guild_api.admin_config(k, val)
                            except Exception as ex:
                                set_status("config: %r" % ex)
                                return
                            if r.get("ok"):
                                set_status("%s saved" % k)
                                Clock.schedule_once(lambda dt: refresh(), 0)
                            else:
                                set_status("config: %s" % r.get("error"))
                        threading.Thread(target=save_work, daemon=True).start()
                    save.bind(on_press=lambda inst, k=key, e=entry: do_save(k, e))
                    grid.add_widget(row)
            fetch(self.guild_api.admin_config, render)

        def load_mods():
            def render(res):
                if not res.get("ok"):
                    set_status("mods: %s" % res.get("error", "failed"))
                    return
                mods = res.get("mods") or []
                admins = res.get("admins") or []
                grid.add_widget(Label(
                    text="moderators (%d)  admins (%d)" % (len(mods), len(admins)),
                    size_hint_y=None, height=dp(22), font_size=sp(12),
                    color=(0.85, 0.87, 0.9, 1), halign="left", valign="middle"))
                row = BoxLayout(size_hint_y=None, height=dp(32), spacing=4)
                entry = TextInput(hint_text="user hash (32 hex)",
                                  multiline=False, size_hint_x=0.6)
                row.add_widget(entry)
                addm = Button(text="add mod", size_hint_x=None, width=80,
                              size_hint_y=None, height=scaled(dp(32)),
                              font_size=sp(11), pos_hint={"center_y": 0.5})
                row.add_widget(addm)
                adda = Button(text="add admin", size_hint_x=None, width=90,
                              size_hint_y=None, height=scaled(dp(32)),
                              font_size=sp(11), pos_hint={"center_y": 0.5})
                row.add_widget(adda)
                grid.add_widget(row)

                def do_op(op, target_hash=None):
                    h = (target_hash or (entry.text or "")).strip().lower()
                    if not h:
                        return

                    def op_work():
                        try:
                            r = self.guild_api.admin_mods(op, h)
                        except Exception as ex:
                            set_status("mods: %r" % ex)
                            return
                        if r.get("ok"):
                            Clock.schedule_once(
                                lambda dt: setattr(entry, "text", ""), 0)
                            set_status("%s %s" % (op, h[:12]))
                            Clock.schedule_once(lambda dt: refresh(), 0)
                        else:
                            set_status("mods: %s" % r.get("error"))
                    threading.Thread(target=op_work, daemon=True).start()
                addm.bind(on_press=lambda inst: do_op("add"))
                adda.bind(on_press=lambda inst: do_op("admin_add"))
                for h in sorted(set(mods) | set(admins)):
                    kind = ("admin" if h in admins else "mod")
                    r = BoxLayout(size_hint_y=None, height=dp(26), spacing=4)
                    r.add_widget(Label(text="%s %s" % (kind, h[:16]),
                                       size_hint_x=0.7, halign="left",
                                       valign="middle", font_size=sp(11)))
                    rm = Button(text="remove", size_hint_x=None, width=70,
                                size_hint_y=None, height=scaled(dp(26)),
                                font_size=sp(10), pos_hint={"center_y": 0.5})
                    rm.bind(on_press=lambda inst, hh=h, kk=kind: do_op(
                        "admin_remove" if kk == "admin" else "remove", hh))
                    r.add_widget(rm)
                    grid.add_widget(r)
            fetch(lambda: self.guild_api.admin_mods("list"), render)

        def load_audit():
            def render(res):
                if not res.get("ok"):
                    set_status("audit: %s" % res.get("error", "failed"))
                    return
                entries = res.get("entries") or []
                grid.add_widget(Label(
                    text="audit log (%d recent)" % len(entries),
                    size_hint_y=None, height=dp(22), font_size=sp(12),
                    color=(0.85, 0.87, 0.9, 1), halign="left", valign="middle"))
                for e in entries:
                    grid.add_widget(Label(
                        text="%s  %s  %s" % (e.get("action", ""),
                                             (e.get("target") or "")[:16],
                                             (e.get("detail") or "")[:40]),
                        size_hint_y=None, height=dp(22), font_size=sp(11),
                        color=(0.8, 0.82, 0.86, 1), halign="left",
                        valign="middle"))
            fetch(lambda: self.guild_api.admin_audit(50), render)

        def load_invites():
            """Invite codes (U6b) — create, list, revoke."""
            def render(res):
                if not res.get("ok"):
                    set_status("invites: %s" % res.get("error", "failed"))
                    return
                grid.add_widget(Label(
                    text="invite codes (%d)" % len(res.get("invites") or []),
                    size_hint_y=None, height=dp(22), font_size=sp(12),
                    color=(0.85, 0.87, 0.9, 1), halign="left",
                    valign="middle"))
                row = BoxLayout(size_hint_y=None, height=dp(32), spacing=4)
                label_in = TextInput(hint_text="label (optional)",
                                     multiline=False, size_hint_x=0.45)
                row.add_widget(label_in)
                uses_in = TextInput(hint_text="max uses", multiline=False,
                                    size_hint_x=0.2)
                row.add_widget(uses_in)
                exp_in = TextInput(hint_text="expires in s", multiline=False,
                                   size_hint_x=0.2)
                row.add_widget(exp_in)
                mk = Button(text="create", size_hint_x=None, width=70,
                            size_hint_y=None, height=scaled(dp(32)),
                            font_size=sp(11), pos_hint={"center_y": 0.5})
                row.add_widget(mk)
                grid.add_widget(row)

                def num(txt, default=0):
                    try:
                        return int((txt or "").strip() or default)
                    except ValueError:
                        return default

                def do_create():
                    # Read the widgets on the main thread; the worker must
                    # not touch widget state.
                    label = (label_in.text or "").strip()
                    max_uses = num(uses_in.text)
                    expires_in = num(exp_in.text)

                    def work():
                        try:
                            r = self.guild_api.invites(
                                "create", label=label,
                                max_uses=max_uses,
                                expires_in=expires_in)
                        except Exception as ex:
                            set_status("invites: %r" % ex)
                            return
                        if r.get("ok"):
                            set_status("invite %s created" % r.get("code"))
                            Clock.schedule_once(lambda dt: refresh(), 0)
                        else:
                            set_status("invites: %s" % r.get("error"))
                    threading.Thread(target=work, daemon=True).start()
                mk.bind(on_press=lambda inst: do_create())

                for inv in res.get("invites") or []:
                    ir = BoxLayout(size_hint_y=None, height=dp(26), spacing=4)
                    expires = inv.get("expires_at") or 0
                    exp_txt = "never" if not expires else time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(expires))
                    ir.add_widget(Label(
                        text="%s  %s  uses %s/%s  exp %s" % (
                            inv.get("code", ""), inv.get("label") or "-",
                            inv.get("uses", 0),
                            inv.get("max_uses") or "*", exp_txt),
                        size_hint_x=0.8, halign="left", valign="middle",
                        font_size=sp(10)))
                    rm = Button(text="revoke", size_hint_x=None, width=70,
                                size_hint_y=None, height=scaled(dp(26)),
                                font_size=sp(10), pos_hint={"center_y": 0.5})

                    def do_revoke(code=inv.get("code")):
                        def work():
                            try:
                                r = self.guild_api.invites("revoke",
                                                           code=code)
                            except Exception as ex:
                                set_status("invites: %r" % ex)
                                return
                            if r.get("ok"):
                                set_status("revoked %s" % code)
                                Clock.schedule_once(lambda dt: refresh(), 0)
                            else:
                                set_status("revoke: %s" % r.get("error"))
                        threading.Thread(target=work, daemon=True).start()
                    rm.bind(on_press=lambda inst: do_revoke())
                    ir.add_widget(rm)
                    grid.add_widget(ir)
            fetch(lambda: self.guild_api.invites("list"), render)

        views = [("config", load_config), ("moderators", load_mods),
                 ("audit", load_audit), ("invites", load_invites)]
        current = [0]

        def show(idx):
            current[0] = idx
            # Invalidate any in-flight load from the previous tab so a slow
            # response cannot append its rows to the newly-cleared grid.
            load_gen[0] += 1
            app = App.get_running_app()
            active_bg = app.theme_colors['primary'] if app else (0.36, 0.41, 0.59, 1)
            inactive_bg = app.theme_colors['sidebar_ctrl'] if app else (0.24, 0.26, 0.3, 1)
            for i, btn in enumerate(tab_buttons):
                btn.background_color = active_bg if i == idx else inactive_bg
            grid.clear_widgets()
            views[idx][1]()

        def refresh():
            """Reload the tab that is currently open."""
            show(current[0])

        tab_buttons = []
        for i, (title, _) in enumerate(views):
            app = App.get_running_app()
            inactive_bg = app.theme_colors['sidebar_ctrl'] if app else (0.24, 0.26, 0.3, 1)
            btn = Button(text=title, background_color=inactive_bg,
                         size_hint_y=None, height=scaled(32),
                         font_size=sp(12), pos_hint={"center_y": 0.5})
            btn.bind(on_press=lambda inst, idx=i: show(idx))
            tabs.add_widget(btn)
            tab_buttons.append(btn)
        show(0)
        popup.open()

    def _open_join_prompt(self, on_done=None):
        """Join-code prompt (U6b): shown when the guild's registration
        policy is 'code' and this client has not joined yet."""
        box = BoxLayout(orientation="vertical", spacing=4, padding=6)
        app = App.get_running_app()
        box.add_widget(Label(
            text="This guild requires an invite code.",
            size_hint_y=None, height=24,
            color=app.theme_colors['text'] if app else (1, 1, 1, 1),
            font_size=sp(13)))
        entry = TextInput(hint_text="invite code", multiline=False)
        box.add_widget(entry)
        row = BoxLayout(size_hint_y=None, height=34, spacing=4)
        submit = Button(text="join", size_hint_y=None, height=scaled(34),
                        pos_hint={"center_y": 0.5})
        row.add_widget(submit)
        cancel = Button(text="cancel", size_hint_y=None, height=scaled(34),
                        pos_hint={"center_y": 0.5})
        row.add_widget(cancel)
        box.add_widget(row)
        status = Label(
            text="", size_hint_y=None, height=22,
            color=app.theme_colors['text_muted'] if app else (0.8, 0.82, 0.86, 1),
            font_size=sp(11))
        box.add_widget(status)
        popup = Popup(title="join", content=box, size_hint=(0.7, 0.4))
        self._join_popup = popup

        def do_submit():
            code = (entry.text or "").strip().upper()
            if not code:
                return

            def work():
                try:
                    res = self.guild_api.join(code)
                except Exception as e:
                    msg = "join failed: %r" % e
                    Clock.schedule_once(lambda dt: setattr(status, "text", msg), 0)
                    return
                if res.get("ok") and res.get("joined"):
                    Clock.schedule_once(
                        lambda dt: setattr(status, "text", "joined"), 0)
                    Clock.schedule_once(lambda dt: popup.dismiss(), 0.3)
                    if on_done:
                        Clock.schedule_once(lambda dt: on_done(True), 0.4)
                else:
                    err = res.get("error", "invalid code")
                    Clock.schedule_once(lambda dt: setattr(status, "text", err),
                                        0)
            threading.Thread(target=work, daemon=True).start()

        submit.bind(on_press=lambda inst: do_submit())
        entry.bind(on_text_validate=lambda inst: do_submit())
        cancel.bind(on_press=lambda inst: popup.dismiss())
        popup.open()

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

    def _update_channel_badges(self):
        """Push unread counts onto the sidebar buttons (UI thread)."""
        try:
            grid = self.root.get_screen("main").ids.channel_grid
        except Exception:
            return
        for child in grid.children:
            if isinstance(child, ChannelButton):
                cid = self._channels.get(child.channel_name)
                unread = 0 if cid in self._muted_channels \
                    else self._channel_unread.get(cid, 0)
                child.badge = unread

    # -- context menus (U3) ------------------------------------------------
    def open_context_menu(self, actions, window_pos):
        """Show the Window-level context menu overlay."""
        if not actions:
            return
        if self._ctx_menu is not None:
            self._ctx_menu.dismiss_now()
        self._ctx_menu = ContextMenuOverlay(actions, window_pos)
        Window.add_widget(self._ctx_menu)

    def _copy_text(self, text):
        from kivy.core.clipboard import Clipboard
        Clipboard.copy(text)
        self._set_status("copied to clipboard")

    def _reply_quote(self, username, content):
        """Quote a message into the input bar: '> name: line' per line."""
        lines = (content or "").split("\n")[:3]
        quote = "\n".join("> %s: %s" % (username, ln[:120]) for ln in lines)
        try:
            main = self.root.get_screen("main")
            inp = main.ids.msg_input
            inp.text = (quote + "\n" + (inp.text or "")).strip("\n") + "\n"
            inp.focus = True
        except Exception as e:
            print("reply quote failed:", repr(e))

    def _mention(self, username):
        try:
            main = self.root.get_screen("main")
            inp = main.ids.msg_input
            inp.text = (inp.text or "") + "@%s " % username
            inp.focus = True
        except Exception as e:
            print("mention failed:", repr(e))

    def _toggle_mute_channel(self, name, cid):
        if cid in self._muted_channels:
            self._muted_channels.discard(cid)
            self._set_status("#%s unmuted" % name)
        else:
            self._muted_channels.add(cid)
            self._set_status("#%s muted (unread badge hidden)" % name)
        try:
            cfg = os.path.join(_app_data_dir(), "muted_channels.json")
            with open(cfg, "w") as f:
                json.dump(sorted(self._muted_channels), f)
        except Exception as e:
            print("muted-channel persist failed:", repr(e))
        self._update_channel_badges()

    def _delete_channel(self, name, cid):
        if not self.guild_api:
            return

        def work():
            try:
                res = self.guild_api.channels_delete(cid)
            except Exception as e:
                res = {"ok": 0, "error": repr(e)}
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("delete failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _mod_action(self, method, user_hash, name):
        """Run a moderation call (mute/ban/...) off the UI thread."""
        if not self.guild_api:
            return

        def work():
            try:
                res = getattr(self.guild_api, method)(user_hash)
            except Exception as e:
                res = {"ok": 0, "error": repr(e)}
            ok = res.get("ok")
            err = res.get("error", "unknown")
            Clock.schedule_once(lambda dt: self._set_status(
                ("%s: %s" % (method.replace("mod_", ""), name)) if ok
                else ("%s failed: %s" % (name, err))), 0)
        threading.Thread(target=work, daemon=True).start()

    def _delete_message(self, msg_id):
        if not self.guild_api:
            return

        def work():
            try:
                res = self.guild_api.delete(msg_id)
            except Exception as e:
                res = {"ok": 0, "error": repr(e)}
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("delete failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _open_edit_popup(self, msg_id, content):
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        ti = TextInput(text=content or "", multiline=True, size_hint_y=None,
                       height=dp(90))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(40),
                        spacing=dp(8))
        popup_holder = {}

        def save(*a):
            new = (ti.text or "").strip()
            if not new or not self.guild_api:
                return

            def work():
                try:
                    self.guild_api.edit(msg_id, new)
                except Exception as e:
                    print("edit failed:", repr(e))
            threading.Thread(target=work, daemon=True).start()
            popup_holder["p"].dismiss()
        save_btn = Button(text="save", size_hint_y=None, height=scaled(dp(40)),
                          pos_hint={"center_y": 0.5})
        save_btn.bind(on_press=save)
        cancel_btn = Button(text="cancel", size_hint_y=None,
                            height=scaled(dp(40)), pos_hint={"center_y": 0.5})
        cancel_btn.bind(on_press=lambda *a: popup_holder["p"].dismiss())
        row.add_widget(save_btn)
        row.add_widget(cancel_btn)
        box.add_widget(ti)
        box.add_widget(row)
        popup = Popup(title="edit message", content=box, size_hint=(0.8, 0.45))
        popup_holder["p"] = popup
        self._popup = popup
        popup.open()

    def _copy_invite(self):
        """shades:// share-link for the current hub (connect-screen format)."""
        host = getattr(self, "hub_host", None)
        port = getattr(self, "hub_port", None)
        node = getattr(self, "node_hash", None)
        if not (host and port and node):
            self._set_status("invite unavailable (no hub connection)")
            return
        self._copy_text(invite.build_invite(host, port, node))

    # -- slash commands (U4) -----------------------------------------------
    def _handle_slash(self, text, current_channel):
        """Client-local commands first; everything else goes to the guild's
        shared dispatcher (/api/v1/command) and shows as an ephemeral notice."""
        parts = text[1:].split(" ", 1)
        cmd = (parts[0] or "").lower()
        rest = (parts[1] if len(parts) > 1 else "").strip()
        if cmd == "me" and rest:
            self.send_raw_text("*%s*" % rest)
            return
        if cmd == "join" and rest:
            self._switch_channel(rest.lstrip("#").strip())
            return
        if cmd == "search":
            self.open_search(prefill=rest)
            return
        if cmd == "invite":
            self._copy_invite()
            return
        if not self.guild_api:
            return

        def work():
            try:
                res = self.guild_api.command(text)
            except Exception as e:
                res = {"ok": 0, "error": repr(e)}
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("command failed: %s" % err), 0)
                return
            out = res.get("output") or "(no output)"
            if cmd == "help":
                out = ("client: /me /join /search /invite\n" + out)
            Clock.schedule_once(lambda dt: self._show_notice(out), 0)
        threading.Thread(target=work, daemon=True).start()

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

    def _show_notice(self, text):
        """Ephemeral command output rendered as a system row (not persisted,
        not part of the ledger)."""
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
        except Exception:
            return
        m = {"msg_id": "notice-%f-%d" % (time.time(), len(text)),
             "author_hash": "", "username": "console", "content": text[:2000],
             "ts": time.time(), "reactions": [], "attachments": [],
             "channel_id": self._stream_cid}
        self._render_message(grid, m)
        Clock.schedule_once(lambda dt: self._scroll_to_bottom(), 0.1)

    def _add_channel(self, name):
        main = self.root.get_screen("main")
        grid = main.ids.channel_grid
        btn = ChannelButton(channel_name=name)
        btn.bind(on_press=lambda inst: self._switch_channel(name))
        grid.add_widget(btn)

    def _refresh_channels_ui(self):
        self._ensure_general_channel()

    def _switch_channel(self, name):
        main = self.root.get_screen("main")
        main._current_channel = name
        # Keep the channel header in sync with the active channel.
        try:
            main.ids.channel_header.text = "#" + name
        except Exception:
            pass
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._msg_widgets.clear()
        self._seq = 0
        self._last_ts = 0.0
        cid = self._channel_cid(name)
        # reroute the stream: only the visible channel flows
        self._stream_cid = cid
        self._stream_cursor = None
        kind = self._channel_kind(cid)
        if kind == "site":
            self._open_site_channel(name, cid)
            return
        if kind == "term":
            self._open_term_channel(name, cid)
            return
        self._older.setdefault(cid, {"oldest_id": None, "has_older": True,
                                     "loading": False})
        self._fetch_messages(name)
        self._refresh_members_ui(name)

    # -- site channels (micron pages over RNS; no HTML anywhere) ---------
    def _open_site_channel(self, name, cid):
        """Open a site channel: fetch its manifest, then render the index."""
        self._site_stack = []
        self._site_current = None
        self._site_theme = {}
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            grid.add_widget(self._micron_label("[i]loading site…[/i]"))
        except Exception as e:
            print("site open error:", repr(e))

        def work():
            manifest = {}
            try:
                res = self.guild_api.channels_info(cid)
                if res.get("ok"):
                    manifest = (res.get("channel") or {}).get("manifest") or {}
            except Exception as e:
                print("site manifest error:", repr(e))
            self._site_theme = manifest.get("theme") or {}
            index = manifest.get("index") or "/index.mu"
            Clock.schedule_once(lambda dt: self._site_load_page(cid, index), 0)
        threading.Thread(target=work, daemon=True).start()

    def _open_term_channel(self, name, cid):
        """Open (or reuse) a guild console session and show the terminal."""
        self._term_page = None
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            page = TermPage()
            self._term_page = page
            page.append("(connecting to the guild console…)\n")
            grid.add_widget(page)
        except Exception as e:
            print("term open error:", repr(e))
        if self._term_sessions.get(cid):
            return   # session survives channel switches until idle expiry

        def work():
            try:
                res = self.guild_api.term_open(cid)
            except Exception as e:
                print("term open failed:", repr(e))
                res = {"ok": 0, "error": repr(e)}
            if res.get("ok"):
                self._term_sessions[cid] = res["session"]
            else:
                err = res.get("error", "unknown")
                Clock.schedule_once(lambda dt: self._set_status(
                    "console: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _send_term_input(self, text):
        """Send one console line; echo it locally, output rides the stream."""
        cid = self._stream_cid
        sid = self._term_sessions.get(cid) if cid else None
        if not sid:
            self._set_status("console not connected")
            return
        if self._term_page:
            self._term_page.append("> " + text + "\n")

        def work():
            try:
                res = self.guild_api.term_data(sid, text + "\n")
                if not res.get("ok"):
                    err = res.get("error", "unknown")
                    if err == "unknown session":
                        self._term_sessions.pop(cid, None)
                    Clock.schedule_once(lambda dt: self._set_status(
                        "console: %s" % err), 0)
            except Exception as e:
                print("term data failed:", repr(e))
        threading.Thread(target=work, daemon=True).start()

    def _site_load_page(self, cid, path, push=False):
        """Fetch + render one micron page. /page/... targets are the guild's
        own pages (raw request); everything else is a site resource (v1)."""
        if push and self._site_current:
            self._site_stack.append(self._site_current)
        self._site_current = (cid, path)
        if not self.guild_api:
            return

        def work():
            text, err = None, "unknown"
            try:
                if path.startswith("/page/"):
                    raw = self.guild_api.fetch_file(path)
                    if raw is not None:
                        text = raw.decode("utf-8", "replace")
                    else:
                        err = "no response"
                else:
                    res = self.guild_api.site_get(cid, path)
                    if res.get("ok"):
                        text = base64.b64decode(
                            res.get("data_b64") or "").decode("utf-8", "replace")
                    else:
                        err = res.get("error", "not found")
            except Exception as e:
                err = repr(e)
                print("site page fetch failed:", err)
            if text is not None:
                Clock.schedule_once(lambda dt: self._render_site_page(cid, text), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("page failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _render_site_page(self, cid, text):
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        cur = getattr(main, "_current_channel", None)
        if cur and self._channel_cid(cur) != cid:
            return   # user switched away while the page was in flight
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._msg_widgets.clear()
        blocks = micron_parse(text)
        page = SitePage()
        bg = micron_page_bg(blocks, self._site_theme)
        if bg:
            page.bg_color = self._hex6_to_rgba(bg)
        if self._site_stack:
            page.add_widget(self._micron_label(
                "[ref=shades:back][b]< Back[/b][/ref]"))
        for b in blocks:
            t = b["type"]
            if t == "directive":
                continue
            if t == "sep":
                page.add_widget(MicronSep())
                continue
            if t == "heading":
                page.add_widget(self._micron_label(
                    runs_to_markup(b["runs"], _escape), heading=True))
                continue
            if t == "code":
                page.add_widget(self._micron_label(_escape(b["text"]), code=True))
                continue
            runs = b.get("runs") or []
            if len(runs) == 1 and micron_media_target(runs[0].get("link") or ""):
                self._add_site_media(page, cid, runs[0])
            else:
                page.add_widget(self._micron_label(
                    runs_to_markup(runs, _escape), center=b.get("center")))
        grid.add_widget(page)
        try:
            main.ids.scroll.scroll_y = 1.0
        except Exception:
            pass

    def _micron_label(self, markup, heading=False, center=False, code=False):
        lbl = Label(markup=True, text=markup)
        lbl.size_hint_y = None
        lbl.font_size = sp(18) if heading else (sp(13) if code else sp(15))
        lbl.halign = "center" if center else "left"
        lbl.color = (0.62, 0.65, 0.7, 1) if code else (0.88, 0.9, 0.93, 1)
        lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        lbl.bind(texture_size=lambda inst, ts: setattr(
            inst, "height", max(dp(18), ts[1] if ts else 0)))
        lbl.bind(on_ref_press=lambda inst, ref: self._micron_link_tapped(ref))
        return lbl

    @staticmethod
    def _hex6_to_rgba(h):
        """'#88aa88' -> (r, g, b, 1.0) floats."""
        h = (h or "#").lstrip("#")
        if len(h) == 3:
            h = "".join(c + c for c in h)
        if len(h) != 6:
            return (0, 0, 0, 0)
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0, 1.0)

    def _add_site_media(self, container, cid, run):
        """A media-only line: images render inline (MessageEmbed, bytes
        fetched over the link), audio/video as a tappable download row."""
        target = (run.get("link") or "").split("`")[0].strip()
        name = target.rsplit("/", 1)[-1] or "media"
        ext = micron_media_target(target)
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            container.add_widget(self._micron_label(
                "[ref=%s]audio: %s[/ref]" % (_escape(target), _escape(name))))
            return
        emb = MessageEmbed()
        emb.source = ""    # placeholder box until the bytes arrive
        container.add_widget(emb)

        def work():
            blob = None
            try:
                if target.startswith("/file/sites/"):
                    parts = target.split("/", 4)
                    if len(parts) == 5:
                        res = self.guild_api.site_get(parts[3], "/" + parts[4])
                        if res.get("ok"):
                            blob = base64.b64decode(res.get("data_b64") or "")
                else:
                    raw = self.guild_api.fetch_file(target)
                    if raw:
                        blob = raw
            except Exception as e:
                print("site media fetch failed:", repr(e))
            if not blob:
                return
            fpath = self._save_attachment_blob(blob, name)
            Clock.schedule_once(lambda dt: setattr(emb, "source", fpath), 0)
        threading.Thread(target=work, daemon=True).start()

    def _micron_link_tapped(self, target):
        """Dispatch a micron link ref press: back stack, media download,
        intra-site pages, guild /page/... pages, everything else -> status."""
        target = (target or "").split("`")[0].strip()
        if target == "shades:back":
            if self._site_stack:
                cid, path = self._site_stack.pop()
                self._site_load_page(cid, path)
            return
        if micron_media_target(target):
            self._site_download_media(target)
            return
        if target.startswith("/file/sites/") and target.endswith(".mu"):
            parts = target.split("/", 4)
            if len(parts) == 5 and re.match(r"^[0-9a-f]{64}$", parts[3]):
                self._site_load_page(parts[3], "/" + parts[4], push=True)
                return
        if target.startswith("/page/"):
            try:
                main = self.root.get_screen("main")
                name = getattr(main, "_current_channel", "general")
                self._site_load_page(self._channel_cid(name), target, push=True)
            except Exception:
                pass
            return
        self._set_status("link: %s" % (target or "(empty)"))

    def _site_download_media(self, target):
        """Download a media target to the local attachments dir."""
        name = target.rsplit("/", 1)[-1] or "media"
        self._set_status("downloading %s…" % name)

        def work():
            blob = None
            try:
                if target.startswith("/file/sites/"):
                    parts = target.split("/", 4)
                    if len(parts) == 5:
                        res = self.guild_api.site_get(parts[3], "/" + parts[4])
                        if res.get("ok"):
                            blob = base64.b64decode(res.get("data_b64") or "")
                else:
                    raw = self.guild_api.fetch_file(target)
                    if raw:
                        blob = raw
            except Exception as e:
                print("site media download failed:", repr(e))
            if blob:
                fpath = self._save_attachment_blob(blob, name)
                Clock.schedule_once(
                    lambda dt: self._set_status("saved %s" % fpath), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("download failed: %s" % name), 0)
        threading.Thread(target=work, daemon=True).start()


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
            # Friends first (U7): local roster, online state best-effort from
            # the guild presence map (a friend away from the guild shows gray).
            if self._friends:
                grid.add_widget(Label(
                    text="friends (%d)" % len(self._friends),
                    size_hint_y=None, height=dp(22), color=(1, 0.85, 0.5, 1),
                    font_size=sp(12), bold=True))
                for f in self._friends:
                    online = f["user_hash"] in self._active_users
                    grid.add_widget(MemberRow(
                        username=f.get("nickname") or
                        ("user-" + f["user_hash"][:6]),
                        avatar_source="",
                        author_hash=f["user_hash"],
                        presence_color=[0.2, 0.8, 0.2, 1] if online
                        else [0.5, 0.5, 0.5, 1],
                    ))
                grid.add_widget(Label(
                    text="members", size_hint_y=None, height=dp(22),
                    color=(0.7, 0.73, 0.77, 1), font_size=sp(12), bold=True))
            if self._presence_ok:
                for user_hash, info in sorted(
                        self._active_users.items(),
                        key=lambda kv: (kv[1].get("username") or "").lower()):
                    grid.add_widget(MemberRow(
                        username=info.get("username") or ("user-" + user_hash[:6]),
                        avatar_source="",
                        author_hash=user_hash,
                        presence_color=[0.2, 0.8, 0.2, 1],   # green = online
                    ))
                if not self._active_users:
                    # A Label, not a MemberRow: rows are clickable profiles.
                    grid.add_widget(Label(
                        text="nobody online", size_hint_y=None, height=dp(28),
                        color=(0.7, 0.73, 0.77, 1), font_size=sp(12)))
                return
            # Fallback: authors seen this session; presence unknown.
            for h, uname in list(self._members.items())[:50]:
                grid.add_widget(MemberRow(
                    username=uname,
                    avatar_source="",
                    author_hash=h,
                    presence_color=[0.5, 0.5, 0.5, 1],
                ))
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
        cid = self._channel_cid(cid)
        inp.text = ""

        def work():
            res = self.guild_api.post(cid, text,
                                      attachments=[self._pending_attachment]
                                      if getattr(self, "_pending_attachment",
                                                 None) else None)
            if res.get("ok"):
                # optimistically show
                msg_id = res.get("msg_id")
                m = {"msg_id": msg_id, "author_hash": self.my_hash,
                     "username": self.username, "content": text,
                     "ts": now(), "channel_id": cid,
                     # one attachment = [name, mime, blob]; the list holds
                     # those triples, matching the guild's ingest format
                     "attachments": [self._pending_attachment]
                     if getattr(self, "_pending_attachment", None) else None}
                Clock.schedule_once(lambda dt: self._render_message(
                    main.ids.message_grid, m), 0)
                self._pending_attachment = None
            else:
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("send failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _on_key_down(self, window, key, scancode, codepoint, modifier):
        """Keyboard hook for the message input and connect screen.

        Plain Enter sends the message; Shift+Enter inserts a newline
        (the multiline input handles the newline itself). Tab moves focus
        to the next input field on the connect screen. Returns True to
        claim the key when we handled it.
        """
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
    def _add_loading_placeholder(self, row):
        """Show a loading placeholder in the row's embed container."""
        print(
            f"[TRACE] _add_loading_placeholder: "
            f"row={row.msg_id[:12] if row.msg_id else '?'}", flush=True)
        dlog(f"_add_loading_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}")
        lbl = Label(
            text=ATT_PLACEHOLDER_TEXT,
            size_hint_y=None,
            height=dp(200),
            color=(0.7, 0.73, 0.77, 1),
            font_size=sp(12),
        )
        lbl._is_loading_placeholder = True
        row.ids.embed_container.add_widget(lbl)
        row._loading_placeholder = lbl

    def _remove_placeholder(self, row):
        """Remove the loading placeholder from the row's embed container."""
        lbl = getattr(row, "_loading_placeholder", None)
        if lbl is not None:
            print(
                f"[TRACE] _remove_placeholder: "
                f"row={row.msg_id[:12] if row.msg_id else '?'}", flush=True)
            dlog(f"_remove_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}")
            try:
                row.ids.embed_container.remove_widget(lbl)
            except Exception:
                pass
            row._loading_placeholder = None

    def open_attach_dialog(self):
        """Open a window to pick a file and its storage style; OK auto-sends.

        Storage styles:
          * server-stored  - guild keeps the file; client re-fetches bytes on
                             restart (loading placeholder until received).
          * ephemeral (P2P) - bytes sent inline; kept locally so they survive
                             restart, but no server copy exists.
        """
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.uix.popup import Popup

        main = self.root.get_screen("main")
        text = (main.ids.msg_input.text or "").strip()

        chooser = FileChooserListView(
            filters=["*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
                     "*.mp4", "*.mp3", ".pdf", ".txt"]
        )
        style_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        app = App.get_running_app()
        style_box.add_widget(
            Label(text="Store:", size_hint_x=None, width=dp(50),
                  color=app.theme_colors['text'] if app else (1, 1, 1, 1), font_size=sp(12),
                  halign="left", valign="middle"))
        btn_server = Button(text="Server-stored", size_hint_x=None,
                            width=dp(130), size_hint_y=None,
                            height=scaled(dp(44)), pos_hint={"center_y": 0.5})
        btn_ephem = Button(text="Ephemeral (P2P)", size_hint_x=None,
                           width=dp(130), size_hint_y=None,
                           height=scaled(dp(44)), pos_hint={"center_y": 0.5})
        selected = {"style": ATT_SERVER}

        def pick(s):
            selected["style"] = s
            active_bg = app.theme_colors['primary'] if app else (0.42, 0.48, 1.0, 1)
            inactive_bg = app.theme_colors['panel_darker'] if app else (0.25, 0.27, 0.31, 1)
            btn_server.background_color = active_bg if s == ATT_SERVER else inactive_bg
            btn_ephem.background_color = active_bg if s == ATT_EPHEMERAL else inactive_bg

        btn_server.bind(on_press=lambda *_: pick(ATT_SERVER))
        btn_ephem.bind(on_press=lambda *_: pick(ATT_EPHEMERAL))
        pick(ATT_SERVER)
        style_box.add_widget(btn_server)
        style_box.add_widget(btn_ephem)

        btn_ok = Button(text="OK — Send", size_hint_y=None,
                        height=scaled(dp(44)),
                        background_color=app.theme_colors['primary'] if app else (
                            0.42, 0.48, 1.0, 1),
                        color=app.theme_colors['text'] if app else (1, 1, 1, 1))
        btn_cancel = Button(text="Cancel", size_hint_y=None,
                            height=scaled(dp(44)),
                            background_color=app.theme_colors['panel_darker'] if app else (
                                0.25, 0.27, 0.31, 1),
                            color=app.theme_colors['text'] if app else (1, 1, 1, 1))
        btn_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        btn_box.add_widget(btn_ok)
        btn_box.add_widget(btn_cancel)

        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        content.add_widget(Label(
            text="Attach a file — choose how it is stored:",
            size_hint_y=None, height=dp(24),
            color=app.theme_colors['text'] if app else (1, 1, 1, 1),
            font_size=sp(12)))
        content.add_widget(chooser)
        content.add_widget(style_box)
        content.add_widget(btn_box)

        popup = Popup(title="Attach file", content=content, size_hint=(0.9, 0.9),
                      auto_dismiss=False)

        def do_ok(*_):
            sel = chooser.selection
            if not sel:
                self._set_status("No file selected")
                return
            path = sel[0]
            popup.dismiss()
            self._send_attached_file(text, path, selected["style"])

        btn_ok.bind(on_press=do_ok)
        btn_cancel.bind(on_press=lambda *_: popup.dismiss())
        popup.open()

    def _send_attached_file(self, text, path, style):
        """Read the chosen file and send it as an attachment of the given style."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("attach failed: %s" % e)
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = path.split("/")[-1]
        # Client-side size check so the user gets a clear message instead of
        # a silent server rejection. Matches server MAX_ATTACHMENT_SZ (32 MB).
        if len(blob) > 32 * 1024 * 1024:
            self._set_status("attach failed: file too large (%d MB, max 32 MB)" %
                             (len(blob) / 1024 / 1024))
            return
        self._send_with_attachment(text, name, mime, blob, style)

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
            print(f"[TRACE] _send_with_attachment.work: posting to channel={cid}", flush=True)
            dlog(f"_send_with_attachment.work: posting to channel={cid}")
            # v1 path: upload the blob once, post by reference (no re-transfer
            # on retry). Fall back to the legacy inline msgpack post when the
            # guild predates /api/v1.
            res = None
            try:
                up = self.guild_api.blobs_put(name, mime, blob)
                if up.get("ok"):
                    res = self.guild_api.post_v1(
                        cid, text,
                        attachments=[{"name": name, "mime": mime,
                                      "blob_id": up["blob_id"]}])
                else:
                    print("[TRACE] blob upload failed: %s" % up.get("error"), flush=True)
            except Exception as e:
                print("[TRACE] v1 blob path failed, falling back:", repr(e), flush=True)
            if res is None or not res.get("ok"):
                legacy = self.guild_api.post(cid, text, attachments=[[name, mime, blob]])
                res = legacy if (res is None or not res.get("ok")) else res
            print(
                f"[TRACE] _send_with_attachment.work: post response "
                f"ok={res.get('ok')} msg_id={res.get('msg_id')} "
                f"attachment={res.get('attachment')}", flush=True)
            dlog(
                f"_send_with_attachment.work: post response "
                f"ok={res.get('ok')} msg_id={res.get('msg_id')} "
                f"attachment={res.get('attachment')}")
            if not res.get("ok"):
                Clock.schedule_once(
                    lambda dt: self._set_status("send failed: %s" % res.get("error", "unknown")), 0)
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
                main.ids.message_grid, m), 0)
            Clock.schedule_once(
                lambda dt: setattr(main.ids.msg_input, "text", ""), 0)
            self._add_to_history(m)
            self._save_history()

        threading.Thread(target=work, daemon=True).start()

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
            # Look up node's address via guild
            try:
                res = self.guild_api.lookup_node(lookup_hash)
                if not res.get("ok"):
                    Clock.schedule_once(lambda dt: self._set_status(
                        "Node not found: %s" % res.get("error", "unknown")), 0)
                    return
                address = res.get("address")
                port = res.get("port")
                identity_hash = res.get("identity_hash")
            except Exception as e:
                err = "Lookup failed: %s" % e
                Clock.schedule_once(
                    lambda dt: self._set_status(err), 0)
                return

            if not address or not port:
                Clock.schedule_once(lambda dt: self._set_status(
                    "No address for node: %s" % lookup_hash), 0)
                return

            # Connect to remote node via RNS.Link and request profile page
            try:
                nh = bytes.fromhex(lookup_hash)
                nid = RNS.Identity.recall(nh)
                if nid is None:
                    # Try with identity_hash from lookup
                    nid = RNS.Identity(bytes.fromhex(identity_hash)) if identity_hash else None
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
        try:
            self.root.current = "profile"
        except Exception:
            self._set_status("Could not switch to profile screen")

    def _show_profile_popup(self, target_hash, username, profile_text,
                            image_paths=None):
        """Show a remote user's profile page in a popup.
        Displays any images referenced in the profile page
        (![alt](/file/name)) fetched from the remote node.
        """
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.image import Image as KivyImage

        # Remove image references from markdown (images shown separately)
        import re as _re
        clean_text = _re.sub(
            r"!\[([^\]]*)\]\(/file/[^)]+\)", r"\1", profile_text)

        # Convert micron/markdown to Kivy markup
        markup_text = md_to_kivy(clean_text)

        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(10))

        # Show fetched images at the top
        if image_paths:
            for filename, local_path in image_paths.items():
                img = KivyImage(
                    source=local_path,
                    allow_stretch=False,
                    keep_ratio=True,
                    size_hint_y=None,
                    height=dp(200),
                )
                content.add_widget(img)

        scroll = ScrollView()
        label = Label(text=markup_text, markup=True, size_hint_y=None,
                      halign="left", valign="top", text_size=(None, None))
        label.bind(texture_size=lambda *args: label.set_size(label.texture_size))
        scroll.add_widget(label)
        content.add_widget(scroll)

        close_btn = Button(text="Close", size_hint_y=None, height=scaled(dp(40)),
                           background_color=self.theme_colors['sidebar_ctrl'],
                           color=self.theme_colors['text'])
        content.add_widget(close_btn)

        popup = Popup(title="Profile: %s" % (username or target_hash[:12]),
                      content=content, size_hint=(0.8, 0.8),
                      auto_dismiss=False)
        close_btn.bind(on_press=popup.dismiss)
        popup.open()

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

    def open_file_chooser(self, avatar=False):
        """Open a file chooser popup to attach a file or select an avatar."""
        self._avatar_mode = avatar
        chooser = FileChooserListView(filters=["*.png", "*.jpg", "*.jpeg",
                                               "*.gif", "*.webp", "*.mp4",
                                               "*.mp3", "*.pdf", "*.txt"])
        popup = Popup(title="Select file", content=chooser,
                      size_hint=(0.9, 0.9))
        chooser.bind(on_submit=lambda inst, selection, *args:
                     self._on_file_selected(selection, popup))
        popup.open()

    def _on_file_selected(self, selection, popup):
        popup.dismiss()
        if not selection:
            return
        path = selection[0]
        if getattr(self, "_avatar_mode", False):
            self._open_pfp_editor(path)
        else:
            self.attach_file(path)

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

        save_btn.bind(on_press=do_save)
        cancel_btn.bind(on_press=do_cancel)
        popup.open()

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
        else:
            import shutil
            shutil.copy(path, orig_path)
            shutil.copy(path, small_path)

        # Upload 256x256 version to guild as avatar. avatar_upload() blocks
        # on the RNS link, so do it on a worker thread; the local files are
        # saved either way and applied to the UI on the main thread.
        def upload_work():
            try:
                with open(small_path, "rb") as f:
                    blob = f.read()
                res = self.guild_api.avatar_upload(blob)
            except Exception:
                res = None

            def apply_upload(dt):
                if res and res.get("ok"):
                    self._set_status("Avatar updated")
                else:
                    self._set_status("avatar upload failed")
            Clock.schedule_once(apply_upload, 0)
        threading.Thread(target=upload_work, daemon=True).start()

        # Always store paths locally regardless of guild API result —
        # the files are saved to disk even if guild upload fails.
        self.my_avatar_orig = orig_path
        self.my_avatar_small = small_path

        # Apply to profile screen if it exists
        if hasattr(self, "profile_screen"):
            self.profile_screen.target_avatar = small_path
            self.profile_screen.ids.profile_pfp.source = small_path

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

    def attach_file(self, path):
        """Attach a file to the current message input as an upload."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("attach failed: %s" % e)
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = path.split("/")[-1]
        self._pending_attachment = [name, mime, blob]
        self._set_status("Attached: %s (%d bytes)" % (name, len(blob)))

    def upload_avatar(self, path):
        """Upload an avatar image to the guild."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("avatar upload failed: %s" % e)
            return

        # avatar_upload() blocks on the RNS link — keep it off the main thread.
        def work():
            try:
                res = self.guild_api.avatar_upload(blob)
            except Exception:
                res = None

            def apply(dt):
                if res and res.get("ok"):
                    self._set_status("Avatar updated")
                    try:
                        self.profile_screen.target_avatar = path
                        self.profile_screen.ids.profile_pfp.source = path
                    except Exception:
                        pass
                else:
                    self._set_status("avatar upload failed")
            Clock.schedule_once(apply, 0)
        threading.Thread(target=work, daemon=True).start()

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
        except Exception:
            return

        # register() blocks on the RNS link — keep it off the main thread.
        def work():
            try:
                res = self.guild_api.register(new_name, "")
            except Exception as e:
                err = "save failed: %s" % e
                Clock.schedule_once(
                    lambda dt: self._set_status(err), 0)
                return
            if res.get("ok"):
                def apply(dt):
                    self.username = new_name
                    try:
                        self.profile_screen.target_username = new_name
                    except Exception:
                        pass
                    self._set_status("Profile saved")
                Clock.schedule_once(apply, 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status(
                        "save failed: %s" % res.get("error", "unknown")), 0)
        threading.Thread(target=work, daemon=True).start()

    # -- ui helpers ------------------------------------------------------
    def _set_status(self, text):
        try:
            self.root.get_screen("connect").ids.status.text = text
        except Exception:
            pass

    def on_stop(self):
        # Stop every background loop: the poller, the presence ping, and the
        # presence fetcher. Without the last one a reconnect would leak a
        # presence thread per session.
        self._poll_stop.set()
        self._stream_stop.set()
        self._announce_stop.set()
        self._presence_stop.set()
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
