#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via PyRun_SimpleFile with __name__ == "__main__".
stdout/stderr are redirected to Android logcat (tags: python / stderr).

On any failure, the traceback is:
  1. printed to logcat, and
  2. written to <app_storage>/sHades_launch_error.log, and
  3. rendered in a BARE Kivy window (amber text on dark) so the
     phone itself shows the autopsy -- no adb required. Bare Kivy is a
     hard APK dependency (recipe-backed), so this works whenever Python
     itself boots, even if kivy_client/kivymd died at import.

The console self-closes after 5 minutes.
"""

import os
import sys
import time
import traceback


def _log_path():
    try:
        from android.storage import app_storage_path  # p4a only
        return os.path.join(app_storage_path(), "sHades_launch_error.log")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "sHades_launch_error.log")


def _show_on_screen(text):
    """Render a failure traceback in a minimal, dependency-free Kivy window."""
    try:
        import kivy
        kivy.require("2.3.0")
        from kivy.app import App
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.label import Label
        from kivy.core.window import Window
        from kivy.clock import Clock

        Window.clearcolor = (0.02, 0.02, 0.02, 1)

        root = BoxLayout(orientation="vertical", padding=8, spacing=4)
        sv = ScrollView(do_scroll_x=False)
        lbl = Label(
            text=text, color=(1, 0.85, 0.4, 1), font_size="11sp",
            halign="left", valign="top", markup=False,
            size_hint=(1, None),
        )
        lbl.bind(
            width=lambda *a: setattr(lbl, "text_size", (lbl.width, None)),
            texture_size=lambda *a: setattr(
                lbl, "height", max(lbl.texture_size[1], 1)),
        )
        sv.add_widget(lbl)
        root.add_widget(sv)
        root.add_widget(Label(
            text="sHades failed to start -- traceback above -- closes in 5 min",
            size_hint=(1, None), height=24, color=(1, 1, 1, 0.7),
            font_size="10sp",
        ))

        class CrashConsole(App):
            def build(self):
                return root

        app = CrashConsole()
        Clock.schedule_once(lambda dt: app.stop(), 300)
        app.run()
    except Exception:
        # Kivy itself is unusable: logcat is the only channel left.
        traceback.print_exc()


def main():
    """Entry point called from Android activity. All Python execution
    is wrapped so any import/runtime crash is visible on-device."""
    print("PYTHON: main() starting", flush=True)
    try:
        print("PYTHON: importing kivy_client", flush=True)
        from kivy_client import sHadesClientApp
        print("PYTHON: imported kivy_client", flush=True)
        app = sHadesClientApp()
        print("PYTHON: created app", flush=True)
        app.run()
        print("PYTHON: app.run() returned", flush=True)
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        try:
            with open(_log_path(), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        # Keep process alive so the on-screen console can be read.
        time.sleep(30)
        _show_on_screen(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
