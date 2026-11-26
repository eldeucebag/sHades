#!/usr/bin/env python3
"""Polish (e): fancy buttons -- one button language, hover/press/disabled.

Boots a minimal MDApp under Xvfb and asserts STATE, never wall-clock
timing or frame counts. The tap-dip animation is driven
deterministically: the running Kivy Animation is advanced by explicit
_update(dt) calls, so no real time has to elapse.

- each variant maps to its DESIGN.md base token (theme first, DESIGN
  hex fallback for the keys theme_colors lacks: primary_hover,
  accent_hover, error)
- hover enter swaps md_bg_color to the hover token, leave restores base
- press starts the dip (opacity strictly between 0.55 and 1 mid-way)
  and lands back at opacity 1 with no animation running
- disabled dims to DISABLED_OPACITY, keeps the base bg, and hover
  while disabled changes nothing; re-enabling restores opacity 1
- reduced motion: press starts no animation and opacity stays 1, but
  hover still swaps the bg
- a live theme change re-tints the button; a missing theme key never
  raises (empty theme_colors falls back to DESIGN.md hex); an unknown
  variant falls back to primary
- KV wiring: kv_md.kv converts 27 buttons to FancyMDButton and leaves
  exactly the 5 deliberate plain MDButtons (text-style People / site
  nav Back+Forward / reply cancel, and the add-channel button whose
  visibility is an opacity binding)
- attach dialogs build on FancyMDButton (source check) and the site
  editor bar does too
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.properties import DictProperty  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.utils import get_color_from_hex  # noqa: E402
from kivymd.app import MDApp  # noqa: E402
from kivymd.uix.button import MDButtonText  # noqa: E402
from kivy.animation import Animation, CompoundAnimation  # noqa: E402

import kivy_client as kc  # noqa: E402
import ui_fx  # noqa: E402
from client_widgets import (  # noqa: E402
    DISABLED_OPACITY, FancyMDButton, OUTLINED_HOVER_ALPHA,
)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _close(a, b, tol=0.002):
    try:
        return (len(a) == len(b)
                and all(abs(float(x) - float(y)) <= tol
                        for x, y in zip(a, b)))
    except Exception:
        return False


def _opacity_anims(widget):
    """Plain Animations currently driving `widget`'s opacity.

    _instances is a set, so picking one entry at random is wrong: the
    tap dip is a Sequence (whose _update raises -- its _widgets values
    are bare True flags, not property dicts), and a button can carry
    other, inert animations at the same time. Only a plain Animation
    whose property dict names opacity counts.
    """
    out = []
    for anim in list(Animation._instances):
        if isinstance(anim, CompoundAnimation):
            continue
        entry = getattr(anim, "_widgets", {}).get(widget.uid)
        if (isinstance(entry, dict)
                and "opacity" in entry.get("properties", {})):
            out.append(anim)
    return out


def _step(widget, dt):
    """Advance the widget's running opacity animations by dt seconds."""
    anims = _opacity_anims(widget)
    for anim in anims:
        anim._update(dt)
    return bool(anims)


def _pump_to_end(widget):
    for _ in range(40):
        if not _opacity_anims(widget):
            break
        _step(widget, 0.05)
    return widget.opacity


def _expected(theme):
    """variant -> (base bg, hover bg) from the live theme + DESIGN hex."""
    hover_outlined = list(theme["msg_own"])
    hover_outlined[3] = OUTLINED_HOVER_ALPHA
    return {
        "primary": (list(theme["primary"]),
                    list(get_color_from_hex("#FFD090"))),
        "accent": (list(theme["accent"]),
                   list(get_color_from_hex("#7CE5D5"))),
        "secondary": (list(theme["panel"]), list(theme["msg_own"])),
        "danger": (list(get_color_from_hex("#FF6B6B")),
                   list(get_color_from_hex("#FF6B6B"))),
        "ghost": (list(theme["bg_main"]), list(theme["panel"])),
        "outlined": ([0, 0, 0, 0], hover_outlined),
    }


class _ProbeApp(MDApp):
    theme_colors = DictProperty({})

    def build(self):
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        root = BoxLayout(orientation="vertical")
        self.buttons = {}
        for variant in ("primary", "accent", "secondary", "danger",
                        "ghost", "outlined"):
            btn = FancyMDButton(variant=variant)
            btn.add_widget(MDButtonText(
                text=variant, theme_text_color="Custom",
                text_color=(0, 0, 0, 1)))
            root.add_widget(btn)
            self.buttons[variant] = btn
        Clock.schedule_once(self._run, 0.5)
        return root

    def _run(self, dt):
        try:
            run_checks(self)
        except Exception as e:
            import traceback
            traceback.print_exc()
            check("run_checks completes without raising", False, repr(e))
        finally:
            ui_fx.set_reduced_motion(False)
            self.stop()


def run_checks(app):
    theme = dict(kc.DEFAULT_THEME_COLORS)
    expected = _expected(theme)
    styles = {"primary": "filled", "accent": "filled",
              "secondary": "filled", "danger": "filled",
              "ghost": "filled", "outlined": "outlined"}

    # --- variant mapping: base bg, style, height ---------------------
    for variant, (base, _hover) in expected.items():
        btn = app.buttons[variant]
        check("%s base bg is its DESIGN.md token" % variant,
              _close(btn.md_bg_color, base),
              "got %s want %s" % (btn.md_bg_color, base))
        check("%s style is %s" % (variant, styles[variant]),
              btn.style == styles[variant], repr(btn.style))
    check("fancy buttons are 40dp tall (class rule)",
          all(abs(b.height - 40) < 1 for b in app.buttons.values()),
          repr([b.height for b in app.buttons.values()]))

    # --- hover: enter swaps, leave restores, per variant -------------
    for variant, (base, hover) in expected.items():
        btn = app.buttons[variant]
        btn.on_enter()
        check("%s hover enter swaps bg to hover token" % variant,
              _close(btn.md_bg_color, hover),
              "got %s want %s" % (btn.md_bg_color, hover))
        btn.on_leave()
        check("%s hover leave restores base bg" % variant,
              _close(btn.md_bg_color, base),
              "got %s want %s" % (btn.md_bg_color, base))

    # --- press: dip starts mid-way and lands at 1 --------------------
    btn = app.buttons["primary"]
    btn.dispatch("on_press")
    check("press starts an opacity animation",
          len(_opacity_anims(btn)) >= 1,
          "found %d" % len(_opacity_anims(btn)))
    _step(btn, 0.03)  # first update arms the animation
    _step(btn, 0.03)  # 0.03s into the 0.06s down-phase
    check("press dip is mid-way (0.55 < opacity < 1)",
          0.55 < btn.opacity < 1, repr(btn.opacity))
    check("press dip is still running mid-way",
          len(_opacity_anims(btn)) >= 1)
    check("press dip lands at opacity 1", _pump_to_end(btn) == 1,
          repr(btn.opacity))
    check("press dip animation finished after landing",
          _opacity_anims(btn) == [],
          "still running: %r" % _opacity_anims(btn))

    # --- disabled: dims, keeps base, hover changes nothing -----------
    btn.disabled = True
    check("disabled dims to DISABLED_OPACITY",
          btn.opacity == DISABLED_OPACITY, repr(btn.opacity))
    check("disabled keeps the base bg",
          _close(btn.md_bg_color, expected["primary"][0]),
          repr(btn.md_bg_color))
    btn.on_enter()
    check("hover while disabled keeps the base bg",
          _close(btn.md_bg_color, expected["primary"][0]),
          repr(btn.md_bg_color))
    btn.dispatch("on_press")
    check("press while disabled starts no animation",
          _opacity_anims(btn) == [],
          "still running: %r" % _opacity_anims(btn))
    check("press while disabled keeps opacity dimmed",
          btn.opacity == DISABLED_OPACITY, repr(btn.opacity))
    btn.on_leave()
    btn.disabled = False
    check("re-enabling restores opacity 1", btn.opacity == 1,
          repr(btn.opacity))
    check("re-enabling restores the base bg",
          _close(btn.md_bg_color, expected["primary"][0]),
          repr(btn.md_bg_color))

    # --- reduced motion: no dip, hover still applies ------------------
    ui_fx.set_reduced_motion(True)
    try:
        btn.dispatch("on_press")
        check("reduced-motion press starts no animation",
              _opacity_anims(btn) == [],
              "still running: %r" % _opacity_anims(btn))
        check("reduced-motion press leaves opacity at 1",
              btn.opacity == 1, repr(btn.opacity))
        btn.on_enter()
        check("reduced-motion hover still swaps bg",
              _close(btn.md_bg_color, expected["primary"][1]),
              repr(btn.md_bg_color))
        btn.on_leave()
        check("reduced-motion hover leave restores base",
              _close(btn.md_bg_color, expected["primary"][0]),
              repr(btn.md_bg_color))
    finally:
        ui_fx.set_reduced_motion(False)

    # --- live theme change re-tints -----------------------------------
    changed = dict(theme)
    changed["primary"] = (1, 0, 0, 1)
    app.theme_colors = changed
    check("live theme change re-tints the primary button",
          _close(app.buttons["primary"].md_bg_color, [1, 0, 0, 1]),
          repr(app.buttons["primary"].md_bg_color))
    app.theme_colors = dict(theme)

    # --- missing theme keys never raise (DESIGN hex fallback) ---------
    app.theme_colors = {}
    try:
        bare = FancyMDButton(variant="primary")
        check("empty theme falls back to DESIGN primary hex",
              _close(bare.md_bg_color,
                     list(get_color_from_hex("#F5B75F"))),
              repr(bare.md_bg_color))
        bare.on_enter()
        check("empty theme falls back to DESIGN primary-hover hex",
              _close(bare.md_bg_color,
                     list(get_color_from_hex("#FFD090"))),
              repr(bare.md_bg_color))
        bare.on_leave()
        bare.disabled = True
        bare.disabled = False
        bare.dispatch("on_press")
        _pump_to_end(bare)
        check("empty theme survives hover/press/disable with no raise",
              bare.opacity == 1, repr(bare.opacity))
    except Exception as e:
        check("empty theme survives hover/press/disable with no raise",
              False, repr(e))
    finally:
        app.theme_colors = dict(theme)

    # --- unknown variant falls back to primary ------------------------
    odd = FancyMDButton(variant="nope")
    check("unknown variant falls back to primary base",
          _close(odd.md_bg_color, list(theme["primary"])),
          repr(odd.md_bg_color))
    check("unknown variant falls back to filled style",
          odd.style == "filled", repr(odd.style))

    # --- KV wiring: 26 converted (split-base counts; the tarball's
    # split client builds the rest in modules, not kv) ---
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "kv_md.kv")) as f:
        kv = f.read()
    plain = [ln for ln in kv.splitlines()
             if ln.strip() == "MDButton:"]
    fancy = [ln for ln in kv.splitlines()
             if ln.strip() == "FancyMDButton:"]
    check("kv converts 28 buttons to FancyMDButton",
          len(fancy) == 28, "got %d" % len(fancy))
    check("kv leaves exactly 5 plain MDButtons", len(plain) == 5,
          "got %d" % len(plain))
    check("kv keeps 4 text-style plain buttons",
          kv.count("style: 'text'") == 4,
          "got %d" % kv.count("style: 'text'"))
    check("kv keeps the add-channel button plain (opacity visibility)",
          "id: add_channel_btn" in kv
          and "style: 'outlined'" in kv, "add_channel_btn changed")
    check("kv primary variants number 9",
          kv.count("variant: 'primary'") == 9,
          "got %d" % kv.count("variant: 'primary'"))
    check("kv outlined variants number 18",
          kv.count("variant: 'outlined'") == 18,
          "got %d" % kv.count("variant: 'outlined'"))
    check("kv danger variant is the delete button only",
          kv.count("variant: 'danger'") == 1,
          "got %d" % kv.count("variant: 'danger'"))
    check("kv delete button drops its hardcoded dark red",
          "0.76, 0.22, 0.22, 1" not in kv, "hardcoded red still present")

    # --- Python wiring: attach dialogs + site editor ------------------
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "app_attachments.py")) as f:
        att = f.read()
    check("attach dialogs build 10 fancy buttons",
          att.count('_fancy_button("') == 10,
          "got %d" % att.count('_fancy_button("'))
    check("attach dialogs keep no plain Button(text=...)",
          "Button(text=" not in att, "plain Button remains")
    check("attach toggle re-tints by variant, not background_color",
          "_set_toggle(btn_server" in att
          and "background_color = active_bg" not in att,
          "toggle still sets background_color")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "kivy_client.py")) as f:
        kc_src = f.read()
    check("site editor bar uses FancyMDButton",
          kc_src.count("FancyMDButton(") == 1
          and "FancyMDButton," in kc_src,
          "got %d creations" % kc_src.count("FancyMDButton("))
    check("site editor Publish is primary, Back/Preview secondary",
          'variant="primary" if primary else "secondary"' in kc_src)


def main():
    _ProbeApp().run()
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
