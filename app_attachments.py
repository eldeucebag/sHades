"""Attachments and media: blobs, embeds, audio, Android SAF flows, sending.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

from client_widgets import (DownloadRow, FancyMDButton, _AudioAttachRow,
                            _app_data_dir)
import base64
import hashlib
import mimetypes
import os
from ui_scale import scaled
import threading
from client_util import (ATT_EPHEMERAL, ATT_SERVER, dlog)

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.uix.filechooser import FileChooserListView
    from kivy.uix.label import Label
    from kivy.metrics import dp
    from kivy.metrics import sp
    import ui_fx
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e
if KIVY:
    from app_site import MessageEmbed  # noqa: E402, F401


def _fancy_fg(variant):
    """Label colour for a FancyMDButton variant (dark on amber primary,
    white on danger, warm white otherwise); theme first, DESIGN.md
    values as fallbacks. Never raises."""
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


def _fancy_button(variant, text, **kw):
    """Build a FancyMDButton with its MDButtonText child.

    MDButton carries no text itself; the child holds it, and the child
    only honours an assigned text_color when theme_text_color is
    Custom. theme_width Custom keeps an explicitly set width (or a
    size_hint_x of 1) instead of auto-sizing to the text. The label
    is kept as ``_fancy_label`` so toggle code can re-tint it when the
    variant changes.
    """
    from kivymd.uix.button import MDButtonText
    btn = FancyMDButton(variant=variant, theme_width="Custom", **kw)
    label = MDButtonText(text=text, theme_text_color="Custom",
                         text_color=_fancy_fg(variant))
    btn.add_widget(label)
    btn._fancy_label = label
    return btn


def _set_toggle(btn, active):
    """Storage-style toggle: the active choice is primary, the other
    secondary. The label colour follows the variant; the widget owns
    the bg, so no background_color is set here."""
    btn.variant = "primary" if active else "secondary"
    label = getattr(btn, "_fancy_label", None)
    if label is not None:
        label.text_color = _fancy_fg(btn.variant)


class AttachmentsMixin:
    """Attachments and media: blobs, embeds, audio, Android SAF flows, sending."""

    _SAF_ATTACH = 0x5A17

    _SAF_GENERIC = 0x5A18

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

    def _attachments_dir(self):
        d = os.path.join(_app_data_dir(), "attachments")
        os.makedirs(d, exist_ok=True)
        return d

    def _cached_attachment_blob(self, att):
        """Resume-race fix (device log 15): server-stored rows re-fetch on
        every restore, but a resume replay can fire before the guild link
        is usable, the fetch returns empty, and the failure is terminal —
        so a previously rendered embed comes back as 'attachment fetch
        failed'. The blob of every successful fetch is already on disk
        (_save_attachment_blob), so consult the local cache first: one
        candidate matching the sanitized name (and the advertised size,
        when known) renders instantly with zero network. Ambiguous or
        missing → None → the normal fetch path."""
        name = att.get("name") or ""
        if not name:
            return None
        safe = "".join(c for c in name
                       if c.isalnum() or c in "._-") or "attachment"
        try:
            size = int(att.get("size") or 0)
        except Exception:
            size = 0
        try:
            d = self._attachments_dir()
            cands = [f for f in os.listdir(d) if f.endswith("_" + safe)]
        except Exception:
            return None
        if size > 0:
            kept = []
            for f in cands:
                try:
                    if os.path.getsize(os.path.join(d, f)) == size:
                        kept.append(f)
                except OSError:
                    pass
            cands = kept
        if len(cands) != 1:
            return None
        try:
            with open(os.path.join(d, cands[0]), "rb") as f:
                return f.read()
        except Exception:
            return None

    def _att_fetch_attempts_pop(self, msg_id, row):
        """Clear the retry counter of a resolved attachment (cache hit or
        live render), so a later genuine failure gets its full 2-attempt
        budget back; also keeps the lazily built dict from growing."""
        attempts = getattr(self, "_att_fetch_attempts", None)
        if not attempts:
            return
        for key in (msg_id, id(row)):
            attempts.pop(key, None)

    def _atts_link_ok(self):
        """True when the guild link is usable for a request right now.
        Used to pace attachment fetch retries: during boot/resume the
        link can still be establishing (log 15: the replay fetch ran
        before the session came up) — that state gets a retry, not a
        terminal 'fetch failed'."""
        try:
            api = getattr(self, "guild_api", None)
            if api is None:
                return False
            import RNS  # noqa: local import; the app already holds it
            st = getattr(getattr(api, "link", None), "status", None)
            return st is not None and st == RNS.Link.ACTIVE
        except Exception:
            return False

    def _add_attachment_embed(self, row, name, blob):
        """Render one attachment. tay's v0.30 spec: images render inline, or
        audio as play/pause/stop, download, volume and the stream itself
        embedded; video and other files present as tappable download rows
        (embedded video playback is a named follow-up, not hidden)."""
        dlog(
            f"_add_attachment_embed: name={name} "
            f"blob_size={len(blob)} ext={os.path.splitext(name)[1].lower()}")
        kind = self._attachment_kind(name)
        container = row.ids.embed_container
        path = self._save_attachment_blob(blob, name)
        if kind == "image":
            source = path
            if not source:
                import tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=os.path.splitext(name)[1] or ".png",
                                                  delete=False)
                tmp.write(blob)
                tmp.close()
                source = tmp.name
            emb = MessageEmbed(source=source,
                               size_hint_y=None, height=dp(200))
            emb.bind(on_release=lambda w, n=name: self._set_status(
                "%s (%d KB)" % (n, len(blob) // 1024)))
            container.add_widget(emb)
            # Polish group 8: fade the image in once its bytes are on
            # disk. reveal() is opacity-only, so the fullscreen tap
            # and the status-line binding above are untouched.
            ui_fx.reveal(emb)
        elif kind == "audio":
            container.add_widget(
                _AudioAttachRow(path=path, name=name, size=len(blob),
                                player=self))
        else:
            # video + every other file: tappable download row
            # (polish group 6: a real 40px row, whole-row tap target,
            # unsaved/saving/saved/failed states; embedded video
            # playback stays a named follow-up)
            container.add_widget(
                DownloadRow(kind=kind, name=name, size=len(blob),
                            blob=blob, player=self))

    def _download_tap(self, name, blob, msg_id):
        """Touch handler for tap-to-save attachment rows: saves the bytes
        into the app attachments dir and names where in the status line."""

        def handler(widget, touch):
            if widget.collide_point(*touch.pos) and touch.button == "left":
                self._save_and_report(name, blob, widget)
                return True
            return False
        return handler

    def _save_and_report(self, name, blob, widget=None):
        """Save the blob and name where in the status line. Returns the
        saved path, or None on failure (DownloadRow reads it to pick
        its saved / failed state)."""
        path = self._save_attachment_blob(blob, name)
        if path:
            self._set_status("saved %s to app storage (%d KB)"
                             % (name, len(blob) // 1024))
        else:
            self._set_status("save failed for %s" % name)
        return path

    def _play_audio(self, path):
        """Play an embedded sound file (tay's v0.30 player spec):
        android uses the platform MediaPlayer (play/pause/stop/volume
        real); desktop builds fall back to kivy SoundLoader (load,
        play/stop/volume with pause as best effort). One active sound
        at a time: starting a new one stops the previous."""
        try:
            import platform as _pf
            old = getattr(self, "_audio_media", None)
            if old is not None:
                try:
                    old.stop()
                    old.release()
                except Exception:
                    pass
                self._audio_media = None
            if _pf.system() == "Android":
                from jnius import autoclass
                MediaPlayer = autoclass("android.media.MediaPlayer")
                mp = MediaPlayer()
                # load from the app dir; MediaPlayer.setDataSource wants a
                # file descriptor for sandboxed paths (no root, SAF law)
                FileInputStream = autoclass("java.io.FileInputStream")
                fd = FileInputStream(path).getFD()
                mp.setDataSource(fd)
                mp.prepare()
                mp.start()
                self._audio_media = mp
            else:
                from kivy.core.audio import SoundLoader
                snd = SoundLoader.load(path)
                if snd is None:
                    self._set_status("audio format not loadable on desktop")
                    return None
                snd.play()
                self._audio_media = snd
            return self._audio_media
        except Exception as e:
            dlog("audio play failed: %r" % (e,))
            self._set_status("audio play failed: %s" % str(e)[:48])
            return None

    def _att_get_chunked(self, path):
        """Download a stored attachment in server-side slices.

        Receipted mechanics (v0.31): the receive path's single-shot
        request carries one receipt and one deadline; attachments
        fetched with size > 128 KB walk /api/v1/att/get instead, one
        slice per request, each with its own deadline, loop driven by
        the server's {offset, eof} echoes the same way the upload loop
        follows its acks. A slice failure raises with the phase named;
        nothing here is a timer-based retry."""
        api = self.guild_api
        if not api:
            raise OSError("attachment fetch: no guild link")
        blob = b""
        off = 0
        for _ in range(64):          # 64 slices x slice cap >> 10 MB law
            res = api.att_get(path, off)
            if not res.get("ok"):
                raise OSError("attachment slice %d failed: %s"
                              % (off, res.get("error") or res.get("code")
                                 or "unknown"))
            chunk = base64.b64decode(res.get("data_b64") or "")
            if not chunk:
                if res.get("eof"):
                    return blob
                raise OSError("attachment slice %d empty" % off)
            blob += chunk
            off += len(chunk)
            total = res.get("total")
            if total is not None and off >= total:
                return blob
            if res.get("eof"):
                return blob
        raise OSError("attachment fetch exceeded 64 slices at offset %d"
                      % off)

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

        # log 15: cache-first — a blob already on disk (saved by a
        # previous successful fetch) renders with zero network, which
        # also makes the resume race (replay before the link is up)
        # harmless for anything embedded before.
        cached = self._cached_attachment_blob(att)
        if cached:
            print(
                f"[TRACE] _fetch_attachment: cache hit {len(cached)} bytes for {path}",
                flush=True)
            dlog(f"_fetch_attachment: cache hit {len(cached)} bytes for {path}")
            self._att_fetch_attempts_pop(msg_id, row)

            def apply_cached(dt, row=row, name=name, cached=cached):
                try:
                    main = self.root.get_screen("main")
                    if main is None:
                        return
                    if msg_id and msg_id not in self._msg_widgets:
                        grid = main.ids.message_grid
                        if grid is not None and row.parent is None:
                            grid.add_widget(row)
                    self._add_attachment_embed(row, name, cached)
                except Exception as e:
                    print("[TRACE] cached embed failed: %r" % (e,),
                          flush=True)
                    dlog(f"cached embed failed: {e!r}")
            Clock.schedule_once(apply_cached, 0)
            return

        self._add_loading_placeholder(row, name)

        def work():
            print(f"[TRACE] _fetch_attachment.work: fetching {path}", flush=True)
            dlog(f"_fetch_attachment.work: fetching {path}")
            Clock.schedule_once(
                lambda dt: self._set_status(
                    "attachment: fetching %s ..." % name), 0)
            # v0.31: big attachments download in server-side slices, the
            # same chunk shape as the upload law: one receipt per slice
            # with its own deadline instead of the whole file on a single
            # 15s receipt that a multi-MB transfer is guaranteed to
            # overrun (receipted: the embed spun forever, no error).
            # Small attachments keep the single-shot path; every slice
            # failure still surfaces by name.
            try:
                if int(att.get("size") or 0) > 128 * 1024:
                    blob = self._att_get_chunked(path)
                else:
                    blob = self.guild_api.fetch_file(path)
            except Exception as e:
                blob = None
                dlog("_fetch_attachment.work: fetch raised %r" % (e,))
            if not blob:
                print(
                    f"[TRACE] _fetch_attachment.work: fetch returned empty for {path}", flush=True)
                dlog(f"_fetch_attachment.work: fetch returned empty for {path}")
                # log 15: a resume replay can beat the guild link up.
                # Cap the whole row at 2 attempts; pace by readiness.
                try:
                    attempts = getattr(self, "_att_fetch_attempts", None)
                    if attempts is None:
                        attempts = self._att_fetch_attempts = {}
                    key = msg_id or path or id(row)
                    n = attempts.get(key, 0) + 1
                    attempts[key] = n
                    if n < 2:
                        delay = 4.0 if self._atts_link_ok() else 8.0
                        Clock.schedule_once(
                            lambda dt, r=row, m=msg_id, a=att:
                            self._fetch_attachment(r, m, a), delay)
                        print(
                            "[TRACE] _fetch_attachment: retry %d for %s in %.0fs"
                            % (n, path, delay), flush=True)
                        dlog("fetch empty, retry %d for %s in %.0fs"
                             % (n, path, delay))
                        Clock.schedule_once(
                            lambda dt, m="attachment %s: link not ready, "
                            "retrying" % name: self._set_status(m), 0)
                        return
                    attempts.pop(key, None)
                except Exception as e:
                    dlog("retry bookkeeping failed: %r" % (e,))
                Clock.schedule_once(
                    lambda dt, m="attachment fetch failed: %s"
                    % name: self._set_status(m), 0)
                Clock.schedule_once(
                    lambda dt: self._remove_placeholder(row), 0)
                return
            Clock.schedule_once(
                lambda dt: self._set_status(
                    "attachment loaded (%d KB)" % (len(blob) // 1024)), 0)
            print(
                f"[TRACE] _fetch_attachment.work: received {len(blob)} bytes "
                f"for {path}", flush=True)
            dlog(f"_fetch_attachment.work: received {len(blob)} bytes for {path}")
            self._att_fetch_attempts_pop(msg_id, row)

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

    def _add_loading_placeholder(self, row, name=""):
        """Show a loading placeholder in the row's embed container.

        log 15 (retry): a retried fetch re-entered this with the first
        placeholder still up, so the row kept a shimmer skeleton above
        the real embed forever. Adding now REPLACES any live one."""
        print(
            f"[TRACE] _add_loading_placeholder: "
            f"row={row.msg_id[:12] if row.msg_id else '?'}", flush=True)
        dlog(f"_add_loading_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}")
        self._remove_placeholder(row)
        if self._attachment_kind(name) == "image":
            # Polish group 8: images get the shimmer skeleton instead
            # of the text line. The classifier is the same one
            # _add_attachment_embed uses, so the placeholder kind and
            # the embed kind can never disagree. The skeleton is 70px
            # tall by design; it sits centred in a 200px box -- the
            # exact height of the embed that replaces it -- so the row
            # does not jump when the image lands.
            from kivy.uix.anchorlayout import AnchorLayout
            box = AnchorLayout(size_hint_y=None, height=dp(200))
            skel = ui_fx.SkeletonRow()
            skel.size_hint_x = 1
            box.add_widget(skel)
            box._is_loading_placeholder = True
            row.ids.embed_container.add_widget(box)
            row._loading_placeholder = box
            return
        lbl = Label(
            text=self._loading_placeholder_text(name),
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

    @staticmethod
    def _loading_placeholder_text(name):
        """Match tay's v0.31.10 receipt ('loading image' for mp3's): say
        what the row is actually loading, judged by extension. Images keep
        the original wording; everything else gets its own word."""
        ext = (name.rsplit(".", 1)[-1].lower()
               if (name or "").count(".") else "")
        if ext in ("mp3", "wav", "ogg", "flac", "m4a", "opus", "aac", "wma"):
            return "[loading audio…]"
        if ext in ("mp4", "mov", "webm", "mkv", "avi"):
            return "[loading video…]"
        if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            return "[loading image…]"
        return "[loading file…]"

    def _open_attach_dialog_android(self):
        """Android attach flow: system picker + storage-style selector."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.popup import Popup

        main = self.root.get_screen("main")
        text = (main.ids.msg_input.text or "").strip()
        app = App.get_running_app()
        state = {"style": ATT_SERVER, "path": None}

        style_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        style_box.add_widget(
            Label(text="Store:", size_hint_x=None, width=dp(50),
                  color=app.theme_colors['text'] if app else (1, 1, 1, 1),
                  font_size=sp(12), halign="left", valign="middle"))
        btn_server = _fancy_button("secondary", "Server-stored",
                                   size_hint_x=None, width=dp(130),
                                   size_hint_y=None, height=scaled(dp(44)),
                                   pos_hint={"center_y": 0.5})
        btn_ephem = _fancy_button("secondary", "Ephemeral (P2P)",
                                  size_hint_x=None, width=dp(130),
                                  size_hint_y=None, height=scaled(dp(44)),
                                  pos_hint={"center_y": 0.5})

        def pick(s):
            state["style"] = s
            _set_toggle(btn_server, s == ATT_SERVER)
            _set_toggle(btn_ephem, s == ATT_EPHEMERAL)

        btn_server.bind(on_press=lambda *_: pick(ATT_SERVER))
        btn_ephem.bind(on_press=lambda *_: pick(ATT_EPHEMERAL))
        pick(ATT_SERVER)
        style_box.add_widget(btn_server)
        style_box.add_widget(btn_ephem)

        chosen = Label(text="No file picked yet", size_hint_y=None,
                       height=dp(56),
                       color=app.theme_colors['text'] if app else (1, 1, 1, 1),
                       font_size=sp(12), halign="left", valign="middle")
        chosen.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        btn_pick = _fancy_button("secondary",
                                 "Choose file (system picker)",
                                 size_hint_y=None, height=scaled(dp(44)))
        btn_ok = _fancy_button("primary", "OK — Send",
                               size_hint_y=None, height=scaled(dp(44)))
        btn_cancel = _fancy_button("secondary", "Cancel",
                                   size_hint_y=None, height=scaled(dp(44)))
        btn_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        btn_box.add_widget(btn_ok)
        btn_box.add_widget(btn_cancel)

        content = BoxLayout(orientation="vertical", spacing=dp(8),
                            padding=dp(8))
        content.add_widget(Label(
            text="Attach a file — choose how it is stored:",
            size_hint_y=None, height=dp(24),
            color=app.theme_colors['text'] if app else (1, 1, 1, 1),
            font_size=sp(12)))
        content.add_widget(btn_pick)
        content.add_widget(chosen)
        content.add_widget(style_box)
        content.add_widget(btn_box)

        popup = Popup(title="Attach file", content=content,
                      size_hint=(0.9, 0.7), auto_dismiss=False)

        def do_pick(*_):
            def on_fail(reason):
                chosen.text = reason
                chosen.color = (1, 0.5, 0.4, 1)

            def on_file(local):
                state["path"] = local
                chosen.text = os.path.basename(local)
                self._set_status("picked %s (%d KB) — choose storage, then OK"
                                 % (chosen.text,
                                    os.path.getsize(local) // 1024))
            self._saf_pick(self._SAF_ATTACH, on_file, on_fail)

        def do_ok(*_):
            if not state["path"]:
                self._set_status("No file selected")
                return
            popup.dismiss()
            self._send_attached_file(text, state["path"], state["style"])

        btn_pick.bind(on_press=do_pick)
        btn_ok.bind(on_press=do_ok)
        btn_cancel.bind(on_press=lambda *_: popup.dismiss())
        ui_fx.popup_entrance(popup)

    def _android_pick_generic(self, avatar):
        """Android generic picker for open_file_chooser(avatar=...)."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.popup import Popup
        app = App.get_running_app()
        hint = Label(text="Use the system picker to choose a file.",
                     size_hint_y=None, height=dp(48),
                     color=app.theme_colors['text'] if app else (1, 1, 1, 1),
                     font_size=sp(12), halign="left", valign="middle")
        hint.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        btn = _fancy_button("primary", "Choose file (system picker)",
                            size_hint_y=None, height=scaled(dp(44)))
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        box.add_widget(hint)
        box.add_widget(btn)
        popup = Popup(title="Select file", content=box,
                      size_hint=(0.9, 0.5), auto_dismiss=False)

        def do_pick(*_):
            def on_fail(reason):
                hint.text = reason
                hint.color = (1, 0.5, 0.4, 1)

            def on_file(local):
                popup.dismiss()
                self._on_file_selected([local], None)
            self._saf_pick(self._SAF_GENERIC, on_file, on_fail)

        btn.bind(on_press=do_pick)
        ui_fx.popup_entrance(popup)

    def _saf_pick(self, request_code, on_file, on_fail=None):
        """Launch the system picker; on_file(local_path) fires on success,
        on_fail(reason) on any failure. Both run on the kivy thread --
        the activity callback arrives on the Android UI thread and is
        marshalled via Clock (kivy widgets must not be touched from
        foreign threads; property dispatch there is silent-no-op)."""
        from jnius import autoclass
        from android import activity as android_activity
        handlers = getattr(self, "_saf_handlers", None)
        if handlers is None:
            handlers = {}
            self._saf_handlers = handlers
            android_activity.bind(on_activity_result=self._on_saf_result)
        if request_code in handlers:
            self._set_status("file picker already open")
            return
        handlers[request_code] = (on_file, on_fail)
        Intent = autoclass("android.content.Intent")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
        intent.addCategory(Intent.CATEGORY_OPENABLE)
        intent.setType("*/*")
        PythonActivity.mActivity.startActivityForResult(intent, request_code)
        self._set_status("system picker open — choose a file there")

    def _on_saf_result(self, request_code, result_code, intent):
        """Called on the ANDROID UI thread by the p4a activity bridge.
        Never touch kivy state here: snapshot, marshal onto the kivy
        thread, and process in _finish_saf_result."""
        handlers = getattr(self, "_saf_handlers", None)
        if not handlers:
            return
        entry = handlers.pop(request_code, None)
        if entry is None:
            return
        from kivy.clock import Clock
        payload = {
            "request_code": request_code,
            "result_code": result_code,
            "intent": intent,
            "on_file": entry[0],
            "on_fail": entry[1],
        }
        Clock.schedule_once(
            lambda dt: self._finish_saf_result(payload), 0)

    def _finish_saf_result(self, payload):
        """Process a picker result on the kivy thread."""
        # v0.36.10 (tay's blank-black on selection, ANGLE device): after
        # any opaque full-screen picker activity the GL surface sometimes
        # comes back unre-painted. Force a draw now and once the frames
        # settle -- cheap, and both legs (icon/banner) benefit.
        try:
            from kivy.core.window import Window
            Window.request_draw()
            Clock.schedule_once(
                lambda dt: Window.request_draw(), 0.15)
        except Exception:
            pass
        result_code = payload["result_code"]
        intent = payload["intent"]
        on_file = payload["on_file"]
        on_fail = payload["on_fail"]

        def fail(reason):
            dlog("pick fail: %s" % reason)
            self._set_status("pick failed: %s" % reason)
            if on_fail:
                on_fail(reason)

        from jnius import autoclass
        ACT = autoclass("android.app.Activity")
        if result_code != ACT.RESULT_OK:
            fail("E1: cancelled (code %s)" % result_code)
            return
        if intent is None:
            fail("E2: picker returned no data")
            return
        uri = intent.getData()
        if uri is None:
            fail("E3: no file uri in result")
            return
        tail = "file"
        try:
            tail = uri.getLastPathSegment() or "file"
        except Exception:
            pass
        self._set_status("copying %s ..." % os.path.basename(tail))

        def on_file_marshalled(local):
            # v0.25: handlers run on the kivy thread only (thread-marshal
            # law); the copy itself now owns a background thread so a big
            # pick can no longer freeze the UI (tay's v0.24 stall).
            try:
                kb = os.path.getsize(local) // 1024
                self._set_status("picked %s (%d KB)"
                                 % (os.path.basename(local), kb))
            except Exception:
                pass
            on_file(local)

        def copy_work():
            try:
                local = self._saf_copy_uri(uri)
            except Exception as e:
                dlog("saf copy failed: %r" % e)
                reason = "E4: copy failed: %s" % (
                    str(e) or type(e).__name__)
                Clock.schedule_once(lambda dt, r=reason: fail(r), 0)
                return
            Clock.schedule_once(
                lambda dt: on_file_marshalled(local), 0)

        threading.Thread(target=copy_work, daemon=True).start()

    def _saf_copy_uri(self, uri):
        """Copy a content:// uri into the app dir; return the local path.
        No python-side byte arrays: the transfer runs java-side via
        java.nio.file.Files.copy (minapi 26), so the only jnius name
        imported is autoclass -- the one proven to import on-device.
        (v0.15 raised E4 ImportError importing jbyte/jarray here.)"""
        try:
            import mimetypes as _mt
        except ImportError:
            _mt = None
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        resolver = PythonActivity.mActivity.getContentResolver()
        # display name, best effort; the jarray(String) cursor query from
        # v0.15 needed the same suspect jnius exports as the byte transfer,
        # so both are gone: last path segment with a mime-based fallback.
        # v0.30: the real display name comes from the resolver's
        # _display_name column -- lastPathSegment is a content id on
        # modern pickers (tay: attachments "still says picked.jpg");
        # the query needs no new jnius exports and falls back on failure.
        name = None
        try:
            cur = resolver.query(uri, ["_display_name"], None, None, None)
            if cur is not None:
                try:
                    if cur.moveToFirst():
                        name = cur.getString(0)
                finally:
                    cur.close()
        except Exception:
            name = None
        if not name:
            try:
                name = uri.getLastPathSegment()
            except Exception:
                name = None
        if name:
            name = os.path.basename(name.replace("\\", "/"))
            if not name or "." not in name:
                name = None
        if not name:
            try:
                mime = resolver.getType(uri)
            except Exception:
                mime = None
            ext = (_mt.guess_extension(mime) if (_mt and mime) else None) or ""
            name = "picked" + ext
        name = os.path.basename(name.replace("\\", "/")) or "picked"
        if name.startswith("."):
            name = "picked" + name
        out_dir = os.path.join(_app_data_dir(), "picked")
        os.makedirs(out_dir, exist_ok=True)
        candidate = name
        stem, ext = os.path.splitext(candidate)
        n = 1
        while os.path.exists(os.path.join(out_dir, candidate)):
            candidate = "%s (%d)%s" % (stem, n, ext)
            n += 1
        target = os.path.join(out_dir, candidate)
        if os.path.exists(target):
            os.remove(target)

        def step(what, fn):
            """Wrap one copy hop so a failure names the hop exactly."""
            try:
                return fn()
            except Exception as e:
                dlog("saf %s failed: %r" % (what, e))
                raise OSError("%s: %s" % (what, type(e).__name__))

        # v0.24 PRIMARY: the file-descriptor path. tay's v0.23 receipt
        # (224113 bytes, head=00 00 00 00 00 00 00 00) caught the
        # java-array trap: the JVM fills ITS copy of the reflect byte
        # array while the python wrapper keeps the original zeros, so
        # the streamed copy had the right size and no content.
        # ParcelFileDescriptor.detachFd() hands over the NATIVE fd int;
        # python os.read owns it end to end and no java byte array
        # crosses the bridge at all.
        pfd = None
        fd = None
        try:
            pfd = step("open-pfd", lambda: resolver.openFileDescriptor(uri, "r"))
        except OSError as e:
            dlog("saf pfd open failed: %s (falling back to streams)" % e)
        if pfd is not None:
            fd = step("detach-fd", lambda: pfd.detachFd())
            if fd is None:
                dlog("saf detachFd returned None")
        if fd is not None:
            total = 0
            try:
                first = os.read(fd, 8)
                if len(first) == 8 and first == b"\x00" * 8:
                    raise OSError("provider served zero-filled bytes "
                                  "(fd head=00 x8)")
                os.lseek(fd, 0, 0)
                with open(target, "wb") as out:
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        total += len(chunk)
                        if total > 10 * 1024 * 1024:
                            raise OSError("picked file exceeds the 32 MB "
                                          "server cap")
            finally:
                try:
                    os.close(fd)
                except Exception:
                    pass
            dlog("saf copy done via fd: %s (%d bytes)"
                 % (target, total))
            transfer = "fd"
        else:
            # legacy stream path (v0.15-v0.23 machinery), fallback only
            stream = step("open-stream", lambda: resolver.openInputStream(uri))
            if stream is None:
                raise OSError("content provider refused read (stream None)")
            transfer = None
            try:
                # primary: java.nio transfer via exact-signature hops only.
                # File(String) ctor and File.toPath() have single overloads --
                # the varargs suspects from v0.16 (getPath/Files.copy) stay
                # quarantined behind this one call.
                jfile = step("jfile", lambda: autoclass("java.io.File")(target))
                jpath = step("jtopath", lambda: jfile.toPath())
                step("niocopy",
                     lambda: autoclass("java.nio.file.Files").copy(stream, jpath))
                transfer = "nio"
            except OSError as e:
                dlog("saf nio path failed: %s" % e)
            if transfer is None:
                # fallback: re-open fresh (nio may have consumed the stream),
                # copy python-side through reflect.Array -- no jnius scalar
                # exports anywhere, no varargs overloads.
                try:
                    stream.close()
                except Exception:
                    pass
                stream2 = step("reopen-stream",
                               lambda: resolver.openInputStream(uri))
                try:
                    step("reflect-copy",
                         lambda: self._reflect_copy(stream2, target))
                finally:
                    try:
                        stream2.close()
                    except Exception:
                        pass
                transfer = "reflect"
            try:
                stream.close()
            except Exception:
                pass
        size = os.path.getsize(target)
        if size > 10 * 1024 * 1024:
            os.remove(target)
            raise OSError("picked file exceeds the 32 MB server cap (%d)"
                          % size)
        # v0.20 integrity gate: a provider stream can return zero bytes
        # (or truncated data) and the transfer still "succeeds" -- every
        # downstream consumer then spins or renders nothing with no
        # error (tay's v0.19 receipt). tay's v0.20 receipt answered the
        # zero-byte branch: 203434 real bytes that pillow cannot
        # decode. v0.21 policy: sniff the magic so the file's TRUE
        # format names itself. A kivy-decodable magic decodes or not in
        # the editor (texture check names it); an undecodable family
        # (HEIC/AVIF) fails here with a message tay can act on.
        if size == 0:
            os.remove(target)
            raise OSError("copy produced a 0-byte file via %s "
                          "(provider stream empty)" % transfer)
        if os.path.splitext(target)[1].lower() \
                in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            magic = self._saf_magic(target)
            if magic is None:
                os.remove(target)
                raise OSError("copy produced unreadable data "
                              "(%d bytes via %s, unknown magic)"
                              % (size, transfer))
            if magic in ("heic", "avif", "unknown"):
                if magic == "unknown" and self._saf_image_ok(target):
                    # extension lied, bytes decode anyway: keep it
                    dlog("saf copy done via %s: %s (%d bytes, "
                         "magic<ext mismatch but PIL verified)"
                         % (transfer, target, size))
                    return target
                # v0.23: HEAD BYTES BEFORE DELETE -- v0.22 removed the
                # file first, so the hex receipt read nothing
                # ('(unreadable)' on tay's flash).
                head_hex = self._saf_head_hex(target)
                os.remove(target)
                if magic == "unknown":
                    raise OSError("picked image is not a decodable format "
                                  "(%d bytes, head=%s) - please pick a "
                                  "JPEG or PNG" % (size, head_hex))
                raise OSError("picked image is %s - kivy cannot render "
                              "that format here, please pick a JPEG "
                              "or PNG (%d bytes via %s, head=%s)"
                              % (magic.upper(), size, transfer, head_hex))
            # known-decodable magic: PIL verify is advisory here. If
            # pillow's decode stack is broken on-device, a hard gate
            # would block good files (v0.20 receipt could be either);
            # the editor's texture check gives the REAL verdict.
            if not self._saf_image_ok(target):
                dlog("saf: PIL could not verify %s magic=%s (%d bytes); "
                     "letting kivy attempt the render"
                     % (target, magic, size))
        dlog("saf copy done via %s: %s (%d bytes, verified)"
             % (transfer, target, size))
        return target

    @staticmethod
    def _saf_head_hex(path, n=8):
        """First n bytes as space-separated hex -- names unknown content
        in a status/error line (v0.22, tay's gallery mystery)."""
        try:
            with open(path, "rb") as f:
                head = f.read(n)
        except Exception:
            return "(unreadable)"
        return " ".join("%02X" % b for b in head)

    @staticmethod
    def _saf_magic(path):
        """Sniff the file's format from its first bytes: jpeg/png/gif/
        webp/heic/avif/pdf/unknown/None (None = unreadable)."""
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except Exception:
            return None
        if head[:3] == b"\xff\xd8\xff":
            return "jpeg"
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if head[:4] in (b"GIF8",):
            return "gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "webp"
        if head[:4] == b"%PDF":
            return "pdf"
        if head[4:8] == b"ftyp":
            brand = head[8:12].lower()
            if brand.startswith(b"heic") or brand.startswith(b"mif1"):
                return "heic"
            if brand.startswith(b"avif") or brand.startswith(b"avis"):
                return "avif"
            return "heic" if brand[:2] in (b"he", b"ms") else "heic"
        return "unknown"

    def _saf_image_ok(self, path):
        """True iff the local file decodes as an image (pillow, an
        in-repo dependency). Any exception -> False; the caller turns
        it into a named error. Non-image extensions are not checked."""
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
            return True
        except Exception as e:
            dlog("saf image verify failed: %s: %s"
                 % (type(e).__name__, e))
            return False

    def _reflect_copy(self, stream, target):
        """Python-side copy with no jbyte/jarray exports: the byte[] is
        built via java.lang.reflect.Array.newInstance + Byte.TYPE
        (public, documented surface), read via stream.read(buf). Slow
        per-element conversion -- correctness fallback only."""
        from jnius import autoclass
        make = autoclass("java.lang.reflect.Array")
        jbyte_cls = autoclass("java.lang.Byte").TYPE
        buf = make.newInstance(jbyte_cls, 65536)
        with open(target, "wb") as out:
            total = 0
            while True:
                got = stream.read(buf)
                if got <= 0:
                    break
                out.write(bytes(bytearray(buf[:got])))
                total += got
                if total > 10 * 1024 * 1024:
                    raise OSError("picked file exceeds the 32 MB server cap")
        return target

    def _android_platform(self):
        from kivy.utils import platform
        return platform == "android"

    def open_attach_dialog(self):
        """Open a window to pick a file and its storage style; OK auto-sends.

        Storage styles:
          * server-stored  - guild keeps the file; client re-fetches bytes on
                             restart (loading placeholder until received).
          * ephemeral (P2P) - bytes sent inline; kept locally so they survive
                             restart, but no server copy exists.
        """
        if self._android_platform():
            self._open_attach_dialog_android()
            return
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
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
        btn_server = _fancy_button("secondary", "Server-stored",
                                   size_hint_x=None, width=dp(130),
                                   size_hint_y=None, height=scaled(dp(44)),
                                   pos_hint={"center_y": 0.5})
        btn_ephem = _fancy_button("secondary", "Ephemeral (P2P)",
                                  size_hint_x=None, width=dp(130),
                                  size_hint_y=None, height=scaled(dp(44)),
                                  pos_hint={"center_y": 0.5})
        selected = {"style": ATT_SERVER}

        def pick(s):
            selected["style"] = s
            _set_toggle(btn_server, s == ATT_SERVER)
            _set_toggle(btn_ephem, s == ATT_EPHEMERAL)

        btn_server.bind(on_press=lambda *_: pick(ATT_SERVER))
        btn_ephem.bind(on_press=lambda *_: pick(ATT_EPHEMERAL))
        pick(ATT_SERVER)
        style_box.add_widget(btn_server)
        style_box.add_widget(btn_ephem)

        btn_ok = _fancy_button("primary", "OK — Send",
                               size_hint_y=None, height=scaled(dp(44)))
        btn_cancel = _fancy_button("secondary", "Cancel",
                                   size_hint_y=None, height=scaled(dp(44)))
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
        ui_fx.popup_entrance(popup)

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
        # a silent server rejection. Matches server MAX_ATTACHMENT_SZ (10 MB).
        if len(blob) > 10 * 1024 * 1024:
            self._set_status("attach failed: file too large (%d MB, max 10 MB)" %
                             (len(blob) / 1024 / 1024))
            return
        self._send_with_attachment(text, name, mime, blob, style)

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

    def _set_upload_progress(self, pct):
        """Drive the composer's upload bar (polish g).

        Runs on the main thread (callers schedule it via Clock from
        the _send_with_attachment worker): pct is the 0-100 value
        from blobs_put's on_progress callback, and any report means
        an upload is in flight, so the bar shows. Progress is state,
        not motion -- the value jumps to the reported pct.
        """
        try:
            pct = int(pct)
        except Exception:
            return
        self.upload_pct = max(0, min(100, pct))
        self.upload_active = True

    def _attach_composer_stage(self, msg):
        # 0.31.10 (tay's boot_3 receipt: "no 'uploading' animation"): the
        # connect-screen status label is invisible from inside a channel,
        # so the whole attach flow ran silent on his screen. The composer's
        # helper text is the progress surface he actually sees: mirror
        # every stage there (upload %, attempts, posting, failure).
        self._set_status(msg)
        if not msg:
            # polish (g): "" is the progress channel being cleared --
            # the attach flow is done on every exit path (success,
            # fallback, failure; the guarded wrapper in
            # _send_with_attachment guarantees the call), so hide the
            # upload bar too. This is the one place that clears it.
            self.upload_active = False
            self.upload_pct = 0
        try:
            main = self.root.get_screen("main")
            main.ids.msg_input.helper_text = msg
        except Exception:
            pass
