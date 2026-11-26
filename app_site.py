"""Micron site channels: page load/render, the site editor, and micron link/media handling.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

import base64
from micron import (micron_media_target, micron_page_bg, micron_parse, runs_to_markup)
import re
import threading
import webbrowser
from client_util import _escape
from client_widgets import MicronRefLabel

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.app import App
    from kivy.uix.image import AsyncImage
    from kivy.properties import BooleanProperty
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.clock import Clock
    from kivy.graphics import Color
    from kivy.properties import ListProperty
    from kivy.graphics import Rectangle
    from kivy.uix.widget import Widget
    from kivy.metrics import dp
    from kivy.metrics import sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e


def _resolve_micron_path(base_path, target):
    """Resolve a micron link target against the current page's path.
    Absolute targets are normalized in place; relative targets resolve
    against the current page's directory. ``..`` pops one segment but
    never escapes the root (the server rejects traversal anyway)."""
    if target.startswith("/"):
        combined = target
    else:
        directory = (base_path or "/").rsplit("/", 1)[0] or "/"
        combined = directory + "/" + target
    parts = []
    for seg in combined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


class SiteMixin:
    """Micron site channels: page load/render, the site editor, and micron link/media handling."""

    def _refresh_site_nav(self, visible=None):
        """Mirror the navigation state into the app properties the KV
        nav bar binds to: visibility, the breadcrumb path, and whether
        Back / Forward have anywhere to go. Call after every navigation,
        render, and error page. visible=None leaves visibility alone."""
        if visible is False:
            self.site_nav_visible = False
            self.site_nav_path = ""
            self.site_nav_can_back = False
            self.site_nav_can_forward = False
            return
        elif visible is True:
            self.site_nav_visible = True
        cur = getattr(self, "_site_current", None)
        if cur:
            cid, path = cur[0], cur[1]
            name = None
            _channel_name = getattr(self, "_channel_name", None)
            if _channel_name is not None:
                try:
                    name = _channel_name(cid)
                except Exception:
                    name = None
            if not name:
                try:
                    name = getattr(self.root.get_screen("main"),
                                   "_current_channel", None)
                except Exception:
                    name = None
            self.site_nav_path = ("#%s %s" % (name, path)) if name else path
        stack = getattr(self, "_site_stack", None) or []
        fwd = getattr(self, "_site_forward", None) or []
        self.site_nav_can_back = bool(stack)
        self.site_nav_can_forward = bool(fwd)

    def _open_site_channel(self, name, cid):
        """Open a site channel: fetch its manifest, then render the index."""
        self._site_stack = []
        self._site_forward = []
        self._site_current = None
        self._site_pending_scroll = 1.0
        self._refresh_site_nav(visible=True)
        self.site_nav_path = "#%s" % name
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

    def _site_load_page(self, cid, path, push=False, restore_y=None):
        """Fetch + render one micron page. /page/... targets are the guild's
        own pages (raw request); everything else is a site resource (v1).
        push records the page being left (with its scroll position) on
        the back stack and drops the forward stack; restore_y is the
        scroll position a Back/Forward navigation restores (None = top)."""
        if push and self._site_current:
            self._site_stack.append(
                (self._site_current[0], self._site_current[1],
                 self._site_scroll_y()))
            fwd = getattr(self, "_site_forward", None)
            if fwd is not None:
                fwd.clear()
        self._site_current = (cid, path)
        self._site_pending_scroll = 1.0 if restore_y is None else restore_y
        # (d) the breadcrumb shows the target path at once, even while
        # the fetch is still running.
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            _refresh_nav()
        if not self.guild_api:
            return
        # (b) loading state: show it synchronously, on the main thread,
        # before the worker fetch starts -- otherwise the old page sits
        # unchanged for the whole fetch and the tap reads as dead.
        self._site_show_loading(cid)

        def work():
            text, err, can_edit = None, "unknown", False
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
                        can_edit = bool(res.get("can_edit"))
                    else:
                        err = res.get("error", "not found")
                        can_edit = bool(res.get("can_edit"))
            except Exception as e:
                err = repr(e)
                print("site page fetch failed:", err)
            if text is not None:
                Clock.schedule_once(
                    lambda dt: self._render_site_page(cid, text,
                                                      can_edit=can_edit), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._site_empty_page(cid, err,
                                                     can_edit=can_edit), 0)
        threading.Thread(target=work, daemon=True).start()

    def _site_scroll_y(self):
        """The page ScrollView's current position (1.0 = top)."""
        try:
            return float(self.root.get_screen("main").ids.scroll.scroll_y)
        except Exception:
            return 1.0

    def _site_set_scroll(self, y):
        """Restore a scroll position after a render. Set at once, then
        once more on the next clock tick: the layout pass can move the
        ScrollView after the widgets land, undoing a same-tick set."""
        try:
            scroll = self.root.get_screen("main").ids.scroll
        except Exception:
            return
        try:
            scroll.scroll_y = y
            Clock.schedule_once(
                lambda dt, s=scroll, v=y: setattr(s, "scroll_y", v), 0)
        except Exception:
            pass

    @staticmethod
    def _site_entry(entry):
        """Normalize a stack entry to (cid, path, scroll_y); entries
        written before scroll capture carry no third element."""
        cid, path = entry[0], entry[1]
        y = entry[2] if len(entry) > 2 else 1.0
        return cid, path, y

    def _site_go_back(self):
        """Back: pop the back stack, shelve the current page (with its
        scroll position) on the forward stack, render the popped page
        at its recorded scroll position. False when there is nothing
        to go back to."""
        stack = getattr(self, "_site_stack", None) or []
        if not stack:
            return False
        cid, path, y = self._site_entry(stack.pop())
        cur = getattr(self, "_site_current", None)
        if cur:
            fwd = getattr(self, "_site_forward", None)
            if fwd is None:
                fwd = self._site_forward = []
            fwd.append((cur[0], cur[1], self._site_scroll_y()))
        self._site_load_page(cid, path, restore_y=y)
        return True

    def _site_go_forward(self):
        """Forward: the mirror of _site_go_back. False when the forward
        stack is empty (e.g. a fresh navigation already dropped it)."""
        fwd = getattr(self, "_site_forward", None) or []
        if not fwd:
            return False
        cid, path, y = self._site_entry(fwd.pop())
        cur = getattr(self, "_site_current", None)
        if cur:
            stack = getattr(self, "_site_stack", None)
            if stack is None:
                stack = self._site_stack = []
            stack.append((cur[0], cur[1], self._site_scroll_y()))
        self._site_load_page(cid, path, restore_y=y)
        return True

    def site_go_back(self):
        """KV entry point for the persistent Back control."""
        return self._site_go_back()

    def site_go_forward(self):
        """KV entry point for the persistent Forward control."""
        return self._site_go_forward()

    def _site_show_loading(self, cid):
        """Replace the page area with a loading line while a page fetch
        runs. Never clobbers another channel: when the viewer has
        already switched away, the cid no longer matches the open
        channel and the grid is left alone (the stale render is
        dropped by the same guard in _render_site_page)."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        try:
            cur = getattr(main, "_current_channel", None)
            if cur and self._channel_cid(cur) != cid:
                return
        except Exception:
            pass
        try:
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            grid.add_widget(self._micron_label("[i]loading…[/i]"))
        except Exception as e:
            print("site loading render failed:", repr(e))

    def _site_empty_page(self, cid, err, can_edit=False):
        """The page fetch failed: replace the 'loading site' placeholder
        with an honest empty page instead of leaving it forever (tay's
        device receipt, 0.36.10 era). When the server says the viewer
        may edit, the empty state IS the creation door: a not-found
        read for a permitted viewer leads with [ Create index page ].
        channels with a default /index.mu, so this is the exceptional
        branch: a site created before the seed, or a deleted target."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        cur = getattr(main, "_current_channel", None)
        if cur and self._channel_cid(cur) != cid:
            return   # user switched away while the fetch was in flight
        try:
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            page = SitePage()
            page.add_widget(self._micron_label(
                "[b]This site is empty[/b]"))
            # (a) broken-link dead end: the back stack already holds the
            # previous page, so offer the way back here too -- without
            # it the viewer is stranded on the error page.
            if getattr(self, "_site_stack", None):
                page.add_widget(self._micron_label(
                    "[ref=shades:back][b]< Back[/b][/ref]"))
            if can_edit and (err or "") == "resource not found":
                page.add_widget(self._micron_label(
                    "[ref=shades:site-create][ Create index page ]"
                    "[/ref]"))
            page.add_widget(self._micron_label(
                "The page failed to load: %s" % _escape(err or "unknown")))
            grid.add_widget(page)
            # Polish group 4: the error page is a navigation landing
            # too, so it fades in with the same reveal as a rendered
            # page. reveal() is opacity-only, so layout never moves,
            # and reduced motion lands the page at opacity 1 at once.
            import ui_fx
            ui_fx.reveal(page)
            # getattr: minimal test stubs bind _site_empty_page without
            # the rest of the mixin (see test_link_selfheal).
            _set_scroll = getattr(self, "_site_set_scroll", None)
            if _set_scroll is not None:
                _set_scroll(1.0)
        except Exception as e:
            print("site empty-page render failed:", repr(e))
        # (d) the breadcrumb shows the failed target too, and the
        # Back gate follows the stack (see the Back ref above).
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            try:
                _refresh_nav()
            except Exception:
                pass
        self._set_status("page failed: %s" % (err or "unknown"))

    def _render_site_page(self, cid, text, can_edit=False):
        """Render one micron page into the main grid. Holds the fetched
        source for the P7c editor and, when the server says the caller
        carries SITE_EDIT, leads with the [ Edit page ] affordance."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        cur = getattr(main, "_current_channel", None)
        if cur and self._channel_cid(cur) != cid:
            return   # user switched away while the page was in flight
        self._site_src_text = text
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._remove_skeleton(getattr(self, "_skeleton_cid", None))
        self._msg_widgets.clear()
        blocks = micron_parse(text)
        page = SitePage()
        if self._site_stack:
            page.add_widget(self._micron_label(
                "[ref=shades:back][b]< Back[/b][/ref]"))
        if can_edit:
            page.add_widget(self._micron_label(
                "[ref=shades:site-edit][b][ Edit page ][/b][/ref]"))
        self._micron_blocks_into(page, blocks, cid)
        grid.add_widget(page)
        # Polish group 4: every page render fades in. reveal() is
        # opacity-only, so layout never moves, and reduced motion
        # lands the page at opacity 1 at once.
        import ui_fx
        ui_fx.reveal(page)
        # (d) the breadcrumb and the Back/Forward gates follow the render.
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            _refresh_nav()
        # (e) scroll restore: a Back/Forward navigation recorded the
        # position the page was left at; a fresh navigation goes top.
        y = getattr(self, "_site_pending_scroll", 1.0) or 1.0
        self._site_pending_scroll = 1.0
        # getattr: minimal test stubs may bind _render_site_page without
        # the rest of the mixin (same pattern as _site_empty_page).
        _set_scroll = getattr(self, "_site_set_scroll", None)
        if _set_scroll is not None:
            _set_scroll(y)

    def _micron_blocks_into(self, container, blocks, cid, media_ok=True):
        """The micron block loop shared by the main render and the site
        editor's preview. media_ok=False (editor preview) renders media
        runs as text so the preview never reaches for the network."""
        bg = micron_page_bg(blocks, self._site_theme)
        if bg and isinstance(container, SitePage):
            container.bg_color = self._hex6_to_rgba(bg)
        for b in blocks:
            t = b["type"]
            if t == "directive":
                continue
            if t == "sep":
                container.add_widget(MicronSep())
                continue
            if t == "heading":
                container.add_widget(self._micron_label(
                    runs_to_markup(b["runs"], _escape), heading=True))
                continue
            if t == "code":
                container.add_widget(self._micron_label(
                    _escape(b["text"]), code=True))
                continue
            runs = b.get("runs") or []
            if len(runs) == 1 and micron_media_target(runs[0].get("link") or ""):
                if media_ok:
                    self._add_site_media(container, cid, runs[0])
                else:
                    container.add_widget(self._micron_label(
                        "[i][media: %s][/i]" % _escape(runs[0].get("link") or "")))
            else:
                container.add_widget(self._micron_label(
                    runs_to_markup(runs, _escape), center=b.get("center")))

    def _open_site_editor(self):
        """Enter the micron editor for the page currently on screen."""
        cid, path = self._site_current or (None, None)
        if not cid or not path:
            self._set_status("no page on screen to edit")
            return
        src = getattr(self, "_site_src_text", None)
        if src is None:
            self._set_status("fetching page source…")

            def work():
                txt, err = None, "fetch failed"
                try:
                    res2 = self.guild_api.site_get(cid, path)
                    if res2.get("ok"):
                        txt = base64.b64decode(res2.get("data_b64") or "").decode(
                            "utf-8", "replace")
                    else:
                        err = res2.get("error", "fetch failed")
                except Exception as e2:
                    err = repr(e2)
                Clock.schedule_once(
                    lambda dt: self._enter_site_editor(cid, path, txt,
                                                       err=err), 0)
            threading.Thread(target=work, daemon=True).start()
            return
        self._enter_site_editor(cid, path, src)

    def _enter_site_editor(self, cid, path, src, err=None):
        if src is None:
            self._set_status("page source unavailable: %s" % (err or "unknown"))
            return
        self._edit_cid, self._edit_path = cid, path
        scr = self.root.get_screen("site_editor")
        scr._src.text = src
        scr._status.text = "editing %s" % path
        holder = scr._prev_holder
        holder.clear_widgets()
        holder.parent.scroll_y = 1.0
        self.root.current = "site_editor"

    def _open_new_site_page(self):
        """Empty-state creation door (tay's device report, 0.36.24): a
        permitted viewer lands on a not-found read of a site with no
        stored index; enter the editor with the same starter template
        the server seeds, keyed to the not-found target (_site_current)."""
        cid, path = getattr(self, "_site_current", None) or (None, None)
        if not cid or not path:
            self._set_status("no page target")
            return
        self._edit_cid, self._edit_path = cid, path
        scr = self.root.get_screen("site_editor")
        scr._src.text = self.SITE_STARTER_INDEX_MU
        scr._status.text = "creating %s" % path
        holder = scr._prev_holder
        holder.clear_widgets()
        holder.parent.scroll_y = 1.0
        self.root.current = "site_editor"

    def _site_editor_back(self):
        """Return to the rendered page; _site_current is still live."""
        if self.root is not None:
            self.root.current = "main"

    def _site_editor_preview(self):
        """Render the editor's source into the in-screen preview column;
        media runs stay text so the preview never reaches the network."""
        try:
            scr = self.root.get_screen("site_editor")
        except Exception:
            return
        blocks = micron_parse(scr._src.text)
        holder = scr._prev_holder
        holder.clear_widgets()
        page = SitePage()
        holder.add_widget(page)
        self._micron_blocks_into(page, blocks,
                                 getattr(self, "_edit_cid", None),
                                 media_ok=False)
        scr._status.text = "preview: %d blocks" % len(blocks)

    def _site_editor_publish_done(self, ok, err):
        try:
            scr = self.root.get_screen("site_editor")
        except Exception:
            return
        if ok:
            scr._status.text = "published"
            if self.root is not None:
                self.root.current = "main"
            self._site_load_page(self._edit_cid, self._edit_path)
        else:
            scr._status.text = "publish failed: %s" % err

    def _micron_label(self, markup, heading=False, center=False, code=False):
        lbl = MicronRefLabel(markup=True, text=markup)
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
            if not fpath:
                # save failed (no cache dir etc.): keep the empty box
                # rather than assigning None into MessageEmbed.source
                return

            def _land(dt):
                emb.source = fpath
                # Polish group 8: same fade-in as message embeds.
                import ui_fx
                ui_fx.reveal(emb)

            Clock.schedule_once(_land, 0)
        threading.Thread(target=work, daemon=True).start()

    def _micron_link_tapped(self, target):
        """Dispatch a micron link ref press: back/forward stack, the
        editor affordances, media download, intra-site pages, guild
        /page/... pages, external links, profile-node hashes, and
        relative targets resolved against the current page. Anything
        with no meaning here falls through to a status line."""
        target = (target or "").split("`")[0].strip()
        if target == "shades:back":
            self._site_go_back()
            return
        if target == "shades:forward":
            self._site_go_forward()
            return
        if target == "shades:site-edit":
            self._open_site_editor()
            return
        if target == "shades:site-create":
            self._open_new_site_page()
            return
        if not target:
            self._set_status("link: (empty)")
            return
        # Anchors: micron has no anchor syntax and nothing in the
        # codebase emits '#' targets (no '#' target in sHades.py,
        # micron.py, or md_to_micron) -- there is nothing to scroll
        # to, so an anchor target is an unknown link, not a feature.
        if target.startswith("#"):
            self._set_status("link: %s" % target)
            return
        if target.startswith(("http://", "https://")):
            try:
                webbrowser.open(target)
                self._set_status("opened %s" % target)
            except Exception as e:
                self._set_status("could not open link: %s" % e)
            return
        if re.match(r"^[0-9a-f]{32}$", target):
            self._open_profile_node(target)
            return
        if not target.startswith("/"):
            # Relative target: resolve against the current page, then
            # dispatch the absolute result through the same branches.
            cur = getattr(self, "_site_current", None)
            if not cur:
                self._set_status("link: %s" % target)
                return
            target = _resolve_micron_path(cur[1], target)
        else:
            target = _resolve_micron_path("/", target)
        if micron_media_target(target):
            self._site_download_media(target)
            return
        if target.startswith("/file/sites/") and target.endswith(".mu"):
            parts = target.split("/", 4)
            if len(parts) == 5 and re.match(r"^[0-9a-f]{64}$", parts[3]):
                self._site_load_page(parts[3], "/" + parts[4], push=True)
                return
        if target.startswith("/file/"):
            # A non-media file link (blob .pdf, site doc, attachment):
            # download it like media instead of a dead status line.
            self._site_download_media(target)
            return
        if target.startswith("/page/"):
            try:
                main = self.root.get_screen("main")
                name = getattr(main, "_current_channel", "general")
                self._site_load_page(self._channel_cid(name), target, push=True)
            except Exception:
                pass
            return
        # Any other absolute path is a resource of the site on screen.
        cur = getattr(self, "_site_current", None)
        if cur:
            if target.endswith(".mu"):
                self._site_load_page(cur[0], target, push=True)
                return
            self._site_download_site_resource(cur[0], target)
            return
        self._set_status("link: %s" % target)

    def _open_profile_node(self, target_hash):
        """A bare 32-hex link target is a profile *node* hash -- the
        guild's username_fragment emits exactly these as the target of
        a linked username. Resolve it through presence to a user hash
        + name when the member is known; otherwise hand the node hash
        itself to open_profile, which falls back to it as the lookup
        hash. An unknown node surfaces as open_profile's 'Node not
        found' status, not a dead status line."""
        username = ""
        user_hash = None
        try:
            for uh, info in (getattr(self, "_active_users", None)
                             or {}).items():
                if (info or {}).get("node_hash") == target_hash:
                    user_hash, username = uh, info.get("username") or ""
                    break
        except Exception:
            pass
        try:
            self.open_profile(user_hash or target_hash, username)
        except Exception as e:
            self._set_status("profile link failed: %s" % e)

    def _site_download_site_resource(self, cid, path):
        """Download a site resource addressed by site path (a relative
        link, or an absolute site path that is not a .mu page)."""
        name = path.rsplit("/", 1)[-1] or "file"
        self._set_status("downloading %s…" % name)

        def work():
            blob = None
            try:
                res = self.guild_api.site_get(cid, path)
                if res.get("ok"):
                    blob = base64.b64decode(res.get("data_b64") or "")
            except Exception as e:
                print("site resource download failed:", repr(e))
            if blob:
                fpath = self._save_attachment_blob(blob, name)
                Clock.schedule_once(
                    lambda dt: self._set_status("saved %s" % fpath), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("download failed: %s" % name),
                    0)
        threading.Thread(target=work, daemon=True).start()

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


# Widget classes that moved with this feature: they were
# defined under `if KIVY:` in kivy_client.py and stay that
# way here, so they only exist when Kivy does.
if KIVY:
    class MessageEmbed(ButtonBehavior, AsyncImage):
        """Clickable image embed - click to expand to full screen, click again to minimize.

        ButtonBehavior comes FIRST: with (AsyncImage, ButtonBehavior) the
        MRO put Widget.on_touch_down ahead of ButtonBehavior's, so the
        behavior never grabbed the touch and on_release (the fullscreen
        toggle) never fired from a real tap. Polish group 8b.
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
