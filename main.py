#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via PyRun_SimpleFile with __name__ == "__main__".
stdout/stderr are redirected to Android logcat (tags: python / stderr).

v0.4 diagnostics: everything the process ever prints (including Kivy's
Logger, GL/window startup chatter, and any traceback) is TEE'd from
process start into sHades_boot.log -- external files dir preferred
(browsable by on-phone file managers), app-storage fallback, home
fallback. The used path is recorded INSIDE the log. So the launch story
survives even when rendering is a black screen and no adb exists. On any
failure the traceback is additionally:
  1. printed to logcat, and
  2. appended to the launch log, and
  3. rendered in a BARE Kivy window (amber text on dark) so the
     phone itself shows the autopsy -- no adb required.

The console self-closes after 5 minutes.
"""

import os
import sys
import time
import traceback

_START = time.time()


def _log_path():
    """Browsable launch-log path: external files dir if reachable."""
    try:
        from jnius import autoclass  # p4a only
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        d = act.getExternalFilesDir(None)
        if d is not None:
            return os.path.join(str(d.getAbsolutePath()), "sHades_boot.log")
    except Exception:
        pass
    try:
        from android.storage import app_storage_path  # p4a only
        return os.path.join(app_storage_path(), "sHades_boot.log")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "sHades_boot.log")


_EXPORT_MIN_INTERVAL = 30.0
# tay's device logs (2026-11-25, two boots): main.py runs twice on p4a
# -- once as __main__ via PyRun_SimpleFile and once as the module
# "main" imported by kivy_client -- and each instance carried its own
# stamp window, so 3 exports escaped the 30 s throttle for 3 tries.
# Module state cannot cross instances; a FILE beside the launch log
# can. The stamp holds the epoch of the last attempt and the
# in-flight bit; a hard native death mid-copy is covered by a TTL,
# not by the finally alone.
_EXPORT_IN_FLIGHT_TTL = 60.0


def _stamp_path():
    """Shared throttle/in-flight stamp beside the launch log itself."""
    return _log_path() + ".export"


def _read_stamp():
    """(epoch, in_flight), or (0.0, False) when absent or corrupt."""
    try:
        with open(_stamp_path(), "r", encoding="utf-8") as f:
            parts = f.read().split()
        return float(parts[0]), parts[1] == "1"
    except Exception:
        return 0.0, False


def _write_stamp(in_flight):
    """Never raises; an unwritable stamp degrades to per-attempt flow."""
    try:
        with open(_stamp_path(), "w", encoding="utf-8") as f:
            f.write("%.3f %d\n" % (time.time(), 1 if in_flight else 0))
    except Exception:
        pass


def _export_bootlog_for_user():
    """Mirror sHades_boot.log into the user-visible Downloads collection
    ( tay 2026-11-19: "crashes are difficult to debug when adb is
    unavailable" ). MediaStore.Downloads on API 29+ -- own rows need no
    runtime permission and every file manager lists Downloads; on
    API 26-28 the direct public Downloads dir rides the declared
    WRITE_EXTERNAL_STORAGE. Java-side copy only (the v0.15 lesson:
    jbyte/jarray imports fail on-device; autoclass + Files.copy is the
    proven bridge). Never raises; off-device or degraded bridges are a
    quiet no-op. Returns True when a copy landed in Downloads."""
    try:
        src_path = _log_path()
        with open(src_path, "rb") as f:
            if not f.read(1) and f.seek(0, 2) == 0:
                return False  # nothing to export yet
    except OSError:
        return False
    try:
        from jnius import autoclass  # p4a only
    except Exception:
        return False
    # throttle at the real attempt: a stamp placed before these
    # probes would arm on dev no-ops and suppress a later attempt
    now = time.time()
    stamp_epoch, stamp_in_flight = _read_stamp()
    if stamp_in_flight and now - stamp_epoch < _EXPORT_IN_FLIGHT_TTL:
        return False
    if now - stamp_epoch < _EXPORT_MIN_INTERVAL:
        return False
    # tay's crasher (0.36.22, 2026-11-20): a SLOW boot (>30 s) clears the
    # throttle, and the second export fired from _boot_file inside the
    # live kivy main loop; its native MediaStore copy opened the stream
    # ("export: out-stream opened") and the process died before "nio
    # copy ok" -- no python traceback (main()'s wrapper never saw it).
    # Never run a second native copy while one is in flight; a reentrant
    # jnius/MediaStore call on the UI thread is exactly what killed it.
    # The guard is the SHARED stamp now: it also holds across the two
    # module instances, and the TTL caps an orphaned in-flight bit
    # (process killed mid-copy) so exports can never lock out forever.
    _write_stamp(True)
    try:
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        SDK_INT = int(autoclass("android.os.Build$VERSION").SDK_INT)
        if SDK_INT >= 29:
            resolver = act.getContentResolver()
            DL = autoclass("android.provider.MediaStore$Downloads")
            coll = DL.getContentUri("external_primary")
            uri = None
            # tay 2026-11-25 (log_13): the orphan world. Own-package
            # MediaStore rows die at uninstall, but the Download FILE
            # from a previous install persists on disk. The bare-name
            # insert then collides with that orphan and this provider
            # refuses to uniquify -- IllegalStateException "Failed to
            # build unique file", 3 export misses out of 3 attempts on
            # the chunk-5 boot. So the row search covers the whole
            # export family (bare name and stamped names), newest
            # first, and when no row exists the insert name is
            # timestamped, which cannot collide with the orphan bare
            # name. Once one row exists it is overwritten in place
            # forever -- still no sHades_boot (1).log stacking.
            try:
                cur = resolver.query(
                    coll, ["_id"],
                    "_display_name LIKE 'sHades_boot%.log'",
                    None, "_date_modified DESC")
                try:
                    if cur is not None and cur.moveToFirst():
                        uri = autoclass(
                            "android.content.ContentUris"
                        ).withAppendedId(coll, cur.getLong(0))
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
            except Exception:
                uri = None
            if uri is None:
                cv = autoclass("android.content.ContentValues")()
                # a unique name at insert time: the stamp cannot
                # collide with the foreign orphan that made "Failed to
                # build unique file" permanent on tay's device
                cv.put("_display_name", "sHades_boot_%s.log" % time.strftime(
                    "%Y%m%d_%H%M%S", time.gmtime()))
                cv.put("mime_type", "text/plain")
                uri = resolver.insert(coll, cv)
                if uri is None:
                    return False
            try:
                out = resolver.openOutputStream(uri)
                print("export: out-stream opened", flush=True)
                if out is None:
                    return False
                # Files.copy(Path, OutputStream) is a REAL overload
                # (API 26+); the shipped InputStream/OutputStream pair
                # resolved to nothing and jnius threw on-device. The
                # source is an internal file, so Paths.get() is legal;
                # Path objects pass through jnius by reference.
                p9 = autoclass("java.nio.file.Paths")
                f9 = autoclass("java.nio.file.Files")
                f9.copy(p9.get(src_path), out)
                print("export: nio copy ok", flush=True)
                try:
                    out.close()
                except Exception:
                    pass
                return True
            except Exception as e:
                print("bootlog export failed:", e, flush=True)
                return False
        # API 26-28: legacy public Downloads dir
        env = autoclass("android.os.Environment")
        dest = env.getExternalStoragePublicDirectory(
            env.DIRECTORY_DOWNLOADS)
        dest_path = os.path.join(str(dest.getAbsolutePath()),
                                 "sHades_boot.log")
        with open(src_path, "rb") as fin, open(dest_path, "wb") as fout:
            fout.write(fin.read())
        return True
    except Exception as e:
        print("bootlog export failed:", e, flush=True)
        return False
    finally:
        _write_stamp(False)


class _Tee:
    """Write to the original stream AND the launch log; never raise."""

    def __init__(self, original, logf):
        self._original = original
        self._logf = logf

    def write(self, data):
        try:
            self._logf.write(data)
            self._logf.flush()
        except Exception:
            pass
        try:
            self._original.write(data)
            self._original.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._logf.flush()
        except Exception:
            pass
        try:
            self._original.flush()
        except Exception:
            pass


def _install_tee():
    """Tee stdout/stderr into the launch log from the very start."""
    try:
        logf = open(_log_path(), "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    logf.write("\n===== sHades launch %.0fs after epoch =====\n" % _START)
    logf.write("launch log path: %s\n" % _log_path())
    sys.stdout = _Tee(getattr(sys, "__stdout__", sys.stdout), logf)
    sys.stderr = _Tee(getattr(sys, "__stderr__", sys.stderr), logf)
    print("TEE installed; stdout/stderr also go to the launch log",
          flush=True)


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


def _android_softinput_resize():
    """tay's 0.36.8 boot-log receipt: the keyboard covers the composer.

    Byte-verified on the 0.36.9 APK: p4a develop ships NO
    windowSoftInputMode in AndroidManifest.xml (the attribute is
    absent), and buildozer 1.5.0 has zero code for the
    android.window_soft_input_mode spec key (grepped the wheel); kivy
    itself says 'resize' needs a different java root layout. So the
    spec key was inert string. Apply ADJUST_RESIZE at runtime here
    instead.

    tay's 0.36.11 boot-log receipt (launch 1795162161s): the direct
    call died with ViewRootImpl$CalledFromWrongThreadException --
    main() runs on SDLThread and the window's view hierarchy may only
    be touched from the UI thread ("Expected: SDLActivity Calling:
    SDLThread"). kivy's changeKeyboard survives the SDL thread only
    because it writes statics and InputMethodManager, not the view
    tree. The set is therefore queued onto the UI thread through
    Activity.runOnUiThread with a python-implemented Runnable (jnius
    PythonJavaClass), the same dispatch class p4a uses elsewhere.
    Prints a boot-log receipt either way so the device run stays
    checkable: 'dispatch queued' from the SDL thread, the set receipt
    from the UI thread once the runnable runs, or the named miss.
    """
    try:
        from jnius import autoclass, PythonJavaClass, java_method

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Params = autoclass("android.view.WindowManager$LayoutParams")
        mode = Params.SOFT_INPUT_ADJUST_RESIZE
        if not isinstance(mode, int):
            raise ValueError(
                "SOFT_INPUT_ADJUST_RESIZE is %r, expected int for the "
                "single setSoftInputMode overload" % (mode,))
        activity = PythonActivity.mActivity

        class _UiApply(PythonJavaClass):
            __javainterfaces__ = ["java/lang/Runnable"]

            @java_method("()V")
            def run(self):
                # runnable runs ON the UI thread: ViewRootImpl is
                # created by it, so the set only lands here
                window = activity.getWindow()
                window.setSoftInputMode(mode)
                print("SOFTINPUT: window set to ADJUST_RESIZE", flush=True)

        activity.runOnUiThread(_UiApply())
        print("SOFTINPUT: dispatch queued on ui thread", flush=True)
    except Exception as e:
        # non-android host, a jnius resolution miss, or an unfaithful
        # constant: report, never raise
        print("SOFTINPUT: resize not dispatched (%r)" % (e,), flush=True)


# Shared state of the visible-frame meter: {"base": frame bottom seen
# without any IME (the largest bottom so far), "bottom": the latest
# frame bottom, "live": a reading succeeded at least once}. The kivy
# Clock tick writes AND reads -- every write is on the kivy thread, so
# no cross-thread python runs on Android at all.
_SOFTINPUT = {"base": 0, "bottom": 0, "live": False}


def softinput_base():
    """Frame bottom measured with no IME visible (largest seen)."""
    return _SOFTINPUT["base"]


def softinput_bottom():
    """Latest visible-frame bottom; base - bottom is the IME height."""
    return _SOFTINPUT["bottom"]


def softinput_live():
    """True once the kivy thread has read a frame at least once."""
    return _SOFTINPUT["live"]


# v0.36.14 (launches 1795169468/1795169471s): the OnGlobalLayoutListener
# design KILLED the boot -- its python callback ran on the UI thread on
# every layout pass and collided with the main thread mid-import of
# kivy's pyx machinery; the main thread never came back and the system
# killed the process inside `import kivy` (no logo-copy noise, no
# 'imported kivy_client', no traceback). tay's log 30, launch 30.
# v0.36.15 therefore measures from the KIVY thread on the existing
# 0.25s Clock tick (kivy_client._softinput_tick) -- the exact pattern
# the gesture-exclusions code already runs there -- and this module
# only holds the state the tick writes. A UI-thread python listener on
# Android is retired: it is a boot-killer, not a slow path.


def main():
    """Entry point called from Android activity. All Python execution
    is wrapped so any import/runtime crash is visible on-device."""
    _install_tee()
    # first mirror: the log exists from the tee header onward, so even
    # a pre-app crash leaves a readable copy in Downloads
    try:
        _export_bootlog_for_user()
    except Exception:
        pass
    print("PYTHON: main() starting", flush=True)
    _android_softinput_resize()
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
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write("\n--- TRACEBACK ---\n" + tb)
        except Exception:
            pass
        # mirror the (now fatal) log into Downloads: no adb needed
        try:
            _export_bootlog_for_user()
        except Exception:
            pass
        # Show the traceback immediately; the console itself remains open
        # for five minutes. A delay here left a blank screen after a crash.
        _show_on_screen(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
