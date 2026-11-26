#!/usr/bin/env python3
"""client_voice_prefs.py -- client voice prefs (tx mode, vad, ptt key).

Persistent client-side voice settings, written under the app data dir as
voice.json (same pattern as muted_channels.json / friends.json). tay
2026-11-25 (user settings page): the voice settings page reads and
writes through this module, so the page itself stays rebuildable while
the prefs survive restarts and engine re-creations.

The ptt key entry stores the identifying fields of the kivy Window key
event at bind time: the key code, the scancode, and a display name.
Dispatch matches on the key code and, when both the binding and the
event carry a scancode, on that too (some Android key events arrive
with scancode 0, so a scancode-less event still matches).
"""
import json
import os

DEFAULTS = {
    "mode": "always",        # always | ptt | vad ("voc" in the UI)
    "vad_rms": 800,          # frame RMS gate (~ -32 dBFS on s16 mono)
    "ptt_key": None,         # {"key": int, "scancode": int, "name": str}
}

VAD_MIN, VAD_MAX = 200, 4000

# kivy key codes worth naming in the settings row; letter keys arrive
# with a codepoint and name themselves instead of using this map.
_KEY_NAMES = {
    9: "tab", 13: "enter", 27: "esc", 32: "space",
    273: "up", 274: "down", 275: "right", 276: "left",
    280: "pgup", 281: "pgdn",
    288: "f1", 289: "f2", 290: "f3", 291: "f4", 292: "f5",
}


def prefs_path(app=None):
    """voice.json beside the other client prefs (app data dir)."""
    base = getattr(app, "user_data_dir", None) if app is not None else None
    if not base:
        try:
            from kivy.app import App
            run = App.get_running_app()
            base = getattr(run, "user_data_dir", None) if run else None
        except Exception:
            base = None
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "voice.json")


def _valid_ptt_key(pk):
    if not isinstance(pk, dict) or not isinstance(pk.get("key"), int):
        return None
    key = int(pk["key"])
    name = str(pk.get("name", "")).strip()
    return {
        "key": key,
        "scancode": int(pk.get("scancode", 0) or 0),
        "name": name or ("code %d" % key),
    }


def load_voice_prefs(path=None, app=None):
    """Load + validate; unreadable or bogus content falls back to
    defaults, so a hand-edited voice.json can never wedge the panel."""
    prefs = dict(DEFAULTS)
    if path is None:
        path = prefs_path(app)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            if raw.get("mode") in ("always", "ptt", "vad"):
                prefs["mode"] = raw["mode"]
            try:
                vad = int(raw.get("vad_rms", prefs["vad_rms"]))
                prefs["vad_rms"] = max(VAD_MIN, min(VAD_MAX, vad))
            except (TypeError, ValueError):
                pass
            pk = _valid_ptt_key(raw.get("ptt_key"))
            if pk is not None:
                prefs["ptt_key"] = pk
    except Exception:
        pass
    return prefs


def save_voice_prefs(prefs, path=None, app=None):
    """Persist the validated subset; raises nothing (a read-only data
    dir degrades to session-only prefs)."""
    clean = dict(DEFAULTS)
    clean["mode"] = prefs.get("mode", DEFAULTS["mode"])
    try:
        clean["vad_rms"] = max(VAD_MIN, min(
            VAD_MAX, int(prefs.get("vad_rms", DEFAULTS["vad_rms"]))))
    except (TypeError, ValueError):
        pass
    pk = _valid_ptt_key(prefs.get("ptt_key"))
    clean["ptt_key"] = pk
    if path is None:
        path = prefs_path(app)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=1)
        return True
    except Exception:
        return False


def key_name(key, scancode=0, codepoint=""):
    """Display name for a kivy key event triple."""
    cp = (codepoint or "") if isinstance(codepoint, str) else ""
    if cp.strip() and cp.isprintable():
        return "key '%s'" % cp.strip()
    return _KEY_NAMES.get(int(key)) or ("code %d" % int(key))


def ptt_key_matches(prefs, key, scancode=0):
    """True when this event is the bound PTT key. The scancode check
    only binds when BOTH the saved binding and the event carry one."""
    pk = (prefs or {}).get("ptt_key")
    if not pk:
        return False
    try:
        if int(pk.get("key", -1)) != int(key):
            return False
        bound = int(pk.get("scancode", 0) or 0)
        got = int(scancode or 0)
        if bound and got and bound != got:
            return False
        return True
    except (TypeError, ValueError):
        return False
