"""Menus, dialogs and popups: context actions, popups, search, palette, slash, prompts.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

import invite
import re
from ui_scale import scaled
import threading
import time
from client_util import md_to_kivy

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.clock import Clock
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.popup import Popup
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.metrics import dp
    from kivy.metrics import sp
    import ui_fx
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e


class MenusMixin:
    """Menus, dialogs and popups: context actions, popups, search, palette, slash, prompts."""

    REACTION_PRESETS = ("👍", "👎", "😂", "❤️", "👀", "❓", "😮", "🙃",
                        "🎉", "🔥", "🙏", "👏", "💯", "🤔", "😭", "💀")

    # tay 2026-11-24: voice channels had no create button, yet the
    # server lane already carried kind "voice" (store.CHANNEL_KINDS +
    # the voice join/roster handlers, sHades.py:2212) - the picker
    # alone was missing it.
    CHANNEL_KIND_LABELS = (
        ("text", "chat"),
        ("site (micron)", "site"),
        ("text stream", "term"),
        ("voice", "voice"),
    )

    def _open_create_channel_popup(self):
        """Add a channel (text / site / text stream / voice) -- the add half of
        tay's "the creator should be able to add/delete channels". The
        delete half already exists in the channel context menu. Server
        enforces MANAGE_CHANNEL/ADMIN; a successful create lands in the
        sidebar through the channels_changed push."""
        if not self.guild_api or not self._can_create_channels():
            return
        box = BoxLayout(orientation="vertical", spacing=4, padding=4)
        # Task 2: no in-box title -- it duplicated the popup title
        # ("create channel") and, once the field stopped stretching,
        # its 26px pushed the stack past the content box at 640x480.
        name_in = TextInput(hint_text="channel name (a-z 0-9 _ -)",
                            multiline=False, size_hint_y=None, height=dp(40),
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        box.add_widget(name_in)
        kind_box = BoxLayout(size_hint_y=None, height=scaled(40), spacing=4)
        box.add_widget(kind_box)
        kind_state = ["chat"]  # chosen server kind
        kind_btns = {}

        def make_pick(mine):
            def pick(_btn):
                kind_state[0] = mine
                for key, cb in kind_btns.items():
                    cb.background_color = ((0.2, 0.35, 0.2, 1) if key == mine
                                           else (0.2, 0.2, 0.25, 1))
            return pick

        for label, k in self.CHANNEL_KIND_LABELS:
            btn = Button(text=label, font_size=sp(13),
                         background_color=((0.2, 0.35, 0.2, 1) if k == "chat"
                                           else (0.2, 0.2, 0.25, 1)))
            kind_btns[k] = btn
            btn.bind(on_press=make_pick(k))
            kind_box.add_widget(btn)
        create_btn = Button(text="create", size_hint_y=None, height=scaled(40),
                            font_size=sp(14))
        box.add_widget(create_btn)
        close = Button(text="close", size_hint_y=None, height=scaled(40))
        box.add_widget(close)
        popup = Popup(title="create channel", content=box,
                      size_hint=(0.7, 0.5))
        self._popup = popup

        def do_create(_btn):
            name = (name_in.text or "").strip()
            if not name:
                self._show_notice("channel name required")
                return
            kind = kind_state[0]

            def work():
                try:
                    res = self.guild_api.channels_create(name, kind)
                except Exception as e:
                    err = "create: %r" % e
                    Clock.schedule_once(
                        lambda dt: self._show_notice(err), 0)
                    return
                ok = res.get("ok")
                err = res.get("error", "failed")
                Clock.schedule_once(
                    lambda dt: ((popup.dismiss(),
                                 self._set_status("#%s created" % name))
                                if ok else
                                self._show_notice("create: %s" % err)), 0)

            threading.Thread(target=work, daemon=True).start()

        create_btn.bind(on_press=do_create)
        close.bind(on_press=lambda _b: popup.dismiss())
        ui_fx.popup_entrance(popup)

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
        row = BoxLayout(size_hint_y=None, height=40, spacing=4)
        name_in = TextInput(hint_text="new role name", multiline=False,
                            background_color=self.theme_colors['input_bg'],
                            foreground_color=self.theme_colors['text'],
                            cursor_color=self.theme_colors['text'])
        row.add_widget(name_in)
        create_btn = Button(text="create", size_hint_x=None, width=70,
                            size_hint_y=None, height=scaled(40),
                            font_size=sp(14), pos_hint={"center_y": 0.5})
        row.add_widget(create_btn)
        box.add_widget(row)
        close = Button(text="close", size_hint_y=None, height=scaled(40))
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
        ui_fx.popup_entrance(popup)

    def _open_edit_popup(self, msg_id, content):
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        ti = TextInput(text=content or "", multiline=True, size_hint_y=None,
                       height=dp(90),
                       background_color=self.theme_colors['input_bg'],
                       foreground_color=self.theme_colors['text'],
                       cursor_color=self.theme_colors['text'])
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
        ui_fx.popup_entrance(popup)

    def _open_emoji_picker(self, msg_id):
        """Emoji picker (task 8): a grid of the 16 presets; picking one
        toggles the reaction and closes the picker."""
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        grid = GridLayout(cols=8, size_hint_y=None, spacing=dp(6),
                          padding=[dp(2)] * 2)
        grid.bind(minimum_height=grid.setter("height"))
        for emoji in self.REACTION_PRESETS:
            btn = Button(text=emoji, font_size=sp(18),
                         size_hint_y=None, height=scaled(dp(44)))
            btn.bind(on_press=lambda inst, m=msg_id, em=emoji:
                     (self._toggle_reaction(m, em), popup.dismiss()))
            grid.add_widget(btn)
        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(grid)
        box.add_widget(scroll)
        popup = Popup(title="react", content=box, size_hint=(0.7, 0.5))
        self._popup = popup
        ui_fx.popup_entrance(popup)

    def _open_reaction_popup(self, msg_id):
        """Backward-compatible alias for the emoji picker."""
        self._open_emoji_picker(msg_id)

    def open_search(self, prefill=""):
        try:
            self.root.get_screen("main")
        except Exception:
            return
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        query = TextInput(text=prefill or "", multiline=False, size_hint_y=None,
                          height=dp(40),
                          background_color=self.theme_colors['input_bg'],
                          foreground_color=self.theme_colors['text'],
                          cursor_color=self.theme_colors['text'],
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
        btn = Button(text="search", size_hint_y=None, height=scaled(dp(40)))
        btn.bind(on_press=run_search)
        box.add_widget(query)
        box.add_widget(btn)
        box.add_widget(results)
        popup = Popup(title="search", content=box, size_hint=(0.85, 0.6))
        self._popup = popup
        ui_fx.popup_entrance(popup)

    def _open_motd_popup(self):
        if not self.server_motd:
            return
        ui_fx.popup_entrance(self._build_motd_popup())

    def _build_motd_popup(self):
        """The full-text motd popup (the strip clips long messages)."""
        box = BoxLayout(orientation="vertical", padding=dp(8),
                        spacing=dp(6))
        scroll_box = ScrollView()
        full = Label(markup=True, font_size=sp(13),
                     color=self.theme_colors.get("text",
                                                 (0.92, 0.94, 0.96, 1)))
        full.text = self.server_motd or ""
        full.bind(width=lambda inst, w: setattr(
            inst, "text_size", (w, None)))
        full.bind(texture_size=full.setter("size"))
        scroll_box.add_widget(full)
        box.add_widget(scroll_box)
        close = Button(text="Close", size_hint_y=None, height=dp(40))
        box.add_widget(close)
        popup = Popup(title="Server message", content=box,
                      size_hint=(0.8, 0.5), auto_dismiss=True)
        close.bind(on_press=lambda inst: popup.dismiss())
        return popup

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
        entry = TextInput(hint_text="invite code", multiline=False,
                          size_hint_y=None, height=dp(40),
                          background_color=self.theme_colors['input_bg'],
                          foreground_color=self.theme_colors['text'],
                          cursor_color=self.theme_colors['text'])
        box.add_widget(entry)
        row = BoxLayout(size_hint_y=None, height=40, spacing=4)
        submit = Button(text="join", size_hint_y=None, height=scaled(40),
                        pos_hint={"center_y": 0.5})
        row.add_widget(submit)
        cancel = Button(text="cancel", size_hint_y=None, height=scaled(40),
                        pos_hint={"center_y": 0.5})
        row.add_widget(cancel)
        box.add_widget(row)
        status = Label(
            text="", size_hint_y=None, height=22,
            color=app.theme_colors['text_muted'] if app else (0.8, 0.82, 0.86, 1),
            font_size=sp(11))
        box.add_widget(status)
        # Task 2: 0.5 tall, not 0.4 -- with the code field fixed at
        # 40px the stack is 150px and the 0.4 popup's content box is
        # only ~131px at 640x480, so the heading poked into the title
        # bar.
        popup = Popup(title="join", content=box, size_hint=(0.7, 0.5))
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
        ui_fx.popup_entrance(popup)

    def _open_admin_panel(self):
        """Server config screen: tabs for General / Membership / Channels /
        Roles / Invites, visible per the caller's roles (the server enforces
        everything regardless -- the client gating is UX, not security)."""
        if not self.guild_api or not getattr(self, "i_am_admin", False):
            return
        try:
            from ui_server_config import ServerConfigScreen
            if self.root is not None:
                screen = self.root.get_screen("server_config")
                screen.client = self
                self.root.current = "server_config"
                self._admin_popup = None
                return
            # kv ScreenManager not bound yet (pre-boot, tests, applets):
            # host the same tabbed screen in a popup so the panel opens
            # on every invocation path.
            from kivy.uix.popup import Popup
            screen = ServerConfigScreen(client=self, name="server_config")
            screen.build_tabs()
            popup = Popup(title="server settings", content=screen,
                          size_hint=(0.9, 0.9))
            ui_fx.popup_entrance(popup)
            self._admin_popup = popup
        except Exception as e:
            print("server_config screen failed:", repr(e))
            return

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
        self._render_message(grid, m, animate=True)
        Clock.schedule_once(lambda dt: self._scroll_to_bottom(), 0.1)

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
        ui_fx.popup_entrance(popup)

    def _accept_hub_rows(self, rows, rerender):
        """Apply the fetched /help table to the palette (and drop the
        fetching note on the next render)."""
        self._slash_hub_rows = rows or []
        try:
            note_lbl = self._palette_popup.content.children[0]
            if isinstance(note_lbl, Label):
                note_lbl.text = ("" if rows else
                                 "hub unreachable - client commands only")
        except Exception:
            pass
        if callable(rerender):
            rerender()

    def _slash_client_rows(self):
        """The client-local slash verbs (kivy_client._handle_slash)."""
        return [("/me", "emote an action line"),
                ("/join", "switch to a channel"),
                ("/search", "search channel history"),
                ("/invite", "copy this guild's invite")]

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
        if cmd == "commands":
            # tay's palette correction: /commands opens the slash-command
            # palette itself - the discovery surface for the dispatcher
            # and any future bot/script verbs.
            self.open_command_palette()
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
                out = ("client: /me /join /search /invite /commands\n" + out)
            Clock.schedule_once(lambda dt: self._show_notice(out), 0)
        threading.Thread(target=work, daemon=True).start()

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
        ui_fx.popup_entrance(popup)

    def _copy_text(self, text):
        from kivy.core.clipboard import Clipboard
        Clipboard.copy(text)
        self._set_status("copied to clipboard")

    def _copy_invite(self):
        """shades:// share-link for the current hub (connect-screen format)."""
        host = getattr(self, "hub_host", None)
        port = getattr(self, "hub_port", None)
        node = getattr(self, "node_hash", None)
        if not (host and port and node):
            self._set_status("invite unavailable (no hub connection)")
            return
        self._copy_text(invite.build_invite(host, port, node))

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

    def _invite_to_channel(self, cid, name):
        """Create a channel-targeted invite + copy the shades:// link."""
        if not self.guild_api:
            return

        def work():
            try:
                res = self.guild_api.invites("create",
                                             label="#%s" % name, channel=cid)
            except Exception as e:
                msg = repr(e)
                Clock.schedule_once(
                    lambda dt: self._set_status("invite failed: %s" % msg),
                    0)
                return
            if res.get("ok"):
                link = "shades://join/%s" % res.get("code", "")
                Clock.schedule_once(
                    lambda dt: self._copy_text(link), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("invite: %s"
                                                % res.get("error")), 0)
        threading.Thread(target=work, daemon=True).start()
