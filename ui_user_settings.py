#!/usr/bin/env python3
"""ui_user_settings.py -- user settings VOICE page (tay 2026-11-25).

The user settings submenu is a TAB of the profile config screen
(ConfigScreen.switch_tab hosts this page), not a standalone screen --
v1 shipped as a separate registered screen and, unopened, rendered
nothing (device log 14: the entry could not open anything). This
module is now one page widget: VoiceSettingsPage is added to the
config screen's scroll host by switch_tab and removed when the
appearance tab resumes.

Rows:
  - transmit mode (always / ptt / voc), applied to the live engine and
    persisted via client_voice_prefs into voice.json;
  - voc RMS threshold slider (200-4000) with a live readout;
  - PTT key setup: capture binds the next Window key down (esc
    cancels), the row shows the bound key's name, clear unbinds;
  - on-screen hold-to-talk button: press raises the engine's tx gate,
    release drops it (tay 2026-11-26: the tab carries the tx controls
    rather than only showing state; mirrors the rail's hold control
    for when the channel rail is not open).

Android note: only the key events the platform actually delivers reach
Window.on_key_down; desktop keys and delivered media/hardware buttons
work out of the box, the rest keeps the on-screen hold button.
"""
import client_voice_prefs as prefs_mod
from kivy.app import App
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.slider import Slider

ACTIVE_BG = (0.25, 0.55, 0.90, 1)
IDLE_BG = (0.13, 0.15, 0.18, 1)


def _row_label(text, w=0.42, h=30):
    lbl = Label(text=text, size_hint_x=w, halign="left", valign="middle",
                size_hint_y=None, height=dp(h), font_size=sp(12),
                color=(0.85, 0.87, 0.9, 1))
    lbl.bind(size=lambda inst, s: setattr(inst, "text_size", s))
    return lbl


def _btn(text, width=96, h=36):
    return Button(text=text, size_hint_x=None, width=dp(width),
                  size_hint_y=None, height=dp(h),
                  background_normal="", background_color=IDLE_BG)


def _value_label(text, w=0.24):
    lbl = Label(text=text, size_hint_x=w, halign="left", valign="middle",
                size_hint_y=None, height=dp(30), font_size=sp(12),
                color=(0.65, 0.68, 0.72, 1))
    lbl.bind(size=lambda inst, s: setattr(inst, "text_size", s))
    return lbl


class VoiceSettingsPage(BoxLayout):
    """One page of the profile config tabs: voice tx prefs."""

    def __init__(self, client=None, **kw):
        super().__init__(orientation="vertical", spacing=dp(6), **kw)
        self.size_hint_y = None
        self.bind(minimum_height=self.setter("height"))
        self.client = client
        self._capturing = False
        self._mode_row = None
        self._vad_val = None
        self._ptt_cur = None
        self._cap_btn = None
        self._status = None
        self._build_voice()

    # ---- rows -----------------------------------------------------------

    def _row_mode(self):
        prefs = self._prefs()
        # rebuilds replace the old row instead of stacking a duplicate
        if self._mode_row is not None and self._mode_row.parent is self:
            self.remove_widget(self._mode_row)
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("transmit mode"))
        active = prefs.get("mode", "always")
        for mode, label in (("always", "always"), ("ptt", "ptt"),
                            ("vad", "voc")):
            b = _btn(label, width=84)
            b.background_color = ACTIVE_BG if mode == active else IDLE_BG
            b.bind(on_press=lambda inst, m=mode: self._pick_mode(m))
            row.add_widget(b)
        self._mode_row = row
        self.add_widget(row)

    def _row_vad(self):
        prefs = self._prefs()
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("voc threshold (rms)"))
        val = _value_label(str(prefs.get("vad_rms", 800)), w=0.14)
        sl = Slider(min=prefs_mod.VAD_MIN, max=prefs_mod.VAD_MAX,
                    value=prefs.get("vad_rms", 800))
        sl.bind(value=lambda inst, v: setattr(val, "text", str(int(v))))
        sl.bind(on_release=lambda inst: self._pick_vad(int(inst.value)))
        self._vad_val = val
        row.add_widget(sl)
        row.add_widget(val)
        self.add_widget(row)

    def _row_ptt_key(self):
        prefs = self._prefs()
        # rebuilds replace the old row instead of stacking a duplicate
        if getattr(self, "_ptt_row", None) is not None \
                and self._ptt_row.parent is self:
            self.remove_widget(self._ptt_row)
        pk = prefs.get("ptt_key")
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("ptt key"))
        cur = _value_label(pk["name"] if pk else "none", w=0.2)
        cap = _btn("capture", width=84)
        cap.bind(on_press=lambda inst: self._start_capture(cap, cur))
        clear = _btn("clear", width=70)
        clear.bind(on_press=lambda inst: self._clear_ptt_key(cur))
        self._ptt_cur = cur
        self._ptt_row = row
        self._cap_btn = cap
        row.add_widget(cur)
        row.add_widget(cap)
        row.add_widget(clear)
        self.add_widget(row)

    def _row_hold(self):
        """On-screen hold-to-talk: press raises the engine tx gate,
        release drops it. Mirrors the rail's hold (ptt) control so the
        tab is usable with the channel rail closed."""
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        row.add_widget(_row_label("ptt button (hold)"))
        hold = _btn("hold (ptt)", width=120, h=38)
        hold.bind(on_press=lambda inst: self._hold_down())
        hold.bind(on_release=lambda inst: self._hold_up())
        self._hold_btn = hold
        row.add_widget(hold)
        self.add_widget(row)

    def _hold_down(self):
        engine = self._live_engine()
        fn = getattr(engine, "set_ptt", None) if engine else None
        if fn is None:
            self._say("ptt: no live voice engine")
            return
        fn(True)
        self._say("ptt gate: up")

    def _hold_up(self):
        engine = self._live_engine()
        fn = getattr(engine, "set_ptt", None) if engine else None
        if fn is not None:
            fn(False)
        self._say("ptt gate: dropped")

    def _build_voice(self):
        note = Label(
            text=("mode gates tx in the pacer: always = open mic, "
                  "ptt = the bound key, voc = voice-activated (rms "
                  "gate). esc cancels a capture."),
            size_hint_y=None, height=dp(42), font_size=sp(10),
            color=(0.55, 0.58, 0.62, 1), halign="left", valign="middle")
        note.bind(size=lambda inst, s: setattr(inst, "text_size", s))
        self.add_widget(note)
        self._row_mode()
        self._row_vad()
        self._row_ptt_key()
        self._row_hold()
        self._status = _value_label("", w=1)
        self._status.size_hint_x = 1
        self.add_widget(self._status)

    def _rows_ok(self):
        return (self._mode_row is not None and self._vad_val is not None
                and self._ptt_cur is not None and self._status is not None)

    # ---- actions --------------------------------------------------------

    def _say(self, text):
        if self._status is not None:
            self._status.text = text

    def _live_engine(self):
        return getattr(self.client, "_voice_engine", None)

    def _prefs(self):
        app = App.get_running_app() or self.client
        stored = getattr(app, "_voice_prefs", None)
        if not isinstance(stored, dict):
            app._voice_prefs = prefs_mod.load_voice_prefs(app=app)
            stored = app._voice_prefs
        return stored

    def _save(self, prefs):
        app = App.get_running_app() or self.client
        return prefs_mod.save_voice_prefs(prefs, app=app)

    def _pick_mode(self, mode):
        prefs = self._prefs()
        prefs["mode"] = mode
        ok = self._save(prefs)
        engine = self._live_engine()
        fn = getattr(engine, "set_voice_mode", None) if engine else None
        if fn is not None:
            fn(mode)
        self._row_mode()
        self._say("tx mode: %s%s" % (mode, "" if ok else " (unsaved)"))

    def _pick_vad(self, rms):
        prefs = self._prefs()
        prefs["vad_rms"] = int(rms)
        ok = self._save(prefs)
        engine = self._live_engine()
        if engine is not None:
            try:
                engine.vad_rms = int(rms)
            except Exception:
                pass
        self._say("voc threshold: %d%s" % (int(rms),
                                           "" if ok else " (unsaved)"))

    def _start_capture(self, btn, cur):
        if self._capturing:
            return
        self._capturing = True
        btn.text = "press any key"
        self._say("press the ptt key (esc cancels)")
        try:
            from kivy.core.window import Window
            Window.bind(on_key_down=self._capture_key_down)
        except Exception:
            self._capturing = False
            self._say("no key surface on this platform")

    def _capture_key_down(self, window, key, scancode, codepoint, modifier):
        if not self._capturing:
            return
        try:
            from kivy.core.window import Window
            Window.unbind(on_key_down=self._capture_key_down)
        except Exception:
            pass
        self._capturing = False
        if self._cap_btn is not None:
            self._cap_btn.text = "capture"
        if int(key) == 27:  # esc: cancel, binding untouched
            self._say("ptt key capture cancelled")
            return
        prefs = self._prefs()
        prefs["ptt_key"] = {
            "key": int(key),
            "scancode": int(scancode or 0),
            "name": prefs_mod.key_name(key, scancode, codepoint),
        }
        ok = self._save(prefs)
        self._row_ptt_key()
        self._say("ptt key bound: %s%s"
                  % (prefs["ptt_key"]["name"], "" if ok else " (unsaved)"))

    def stop_capture(self):
        """Tab switch away: cancel a pending capture quietly."""
        if self._capturing:
            self._capturing = False
            try:
                from kivy.core.window import Window
                Window.unbind(on_key_down=self._capture_key_down)
            except Exception:
                pass
            if self._cap_btn is not None:
                self._cap_btn.text = "capture"

    def _clear_ptt_key(self, cur):
        prefs = self._prefs()
        prefs["ptt_key"] = None
        ok = self._save(prefs)
        self._row_ptt_key()
        self._say("ptt key cleared%s" % ("" if ok else " (unsaved)"))
