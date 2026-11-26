#!/usr/bin/env python3
"""Attachment rendering regression tests for the sHades Kivy client.

Three bugs were fixed here:
  1. the guild returns attachment metadata as dicts
     ({"name", "mime", "size", "path"}), but the client only understood the
     local [name, mime, blob] list form, so every attachment received from
     the guild was silently dropped;
  2. the client never downloaded the attachment bytes from the guild's
     /file/attachments/... handler;
  3. the optimistic local render passed the pending attachment as a flat
     list instead of a list of [name, mime, blob] triples, so the sender's
     own attachment was mangled.

Usage:  /usr/bin/python tests/test_attachments.py
"""
import os
import sys
import time
import functools
import tempfile

import base64

# Use the real SDL window on DISPLAY (including CI's Xvfb).

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")


class FakeIds:
    def __init__(self, grid):
        self.message_grid = grid
        self.msg_input = FakeInput()


class FakeInput:
    text = ""


class FakeMain:
    _current_channel = "general"

    def __init__(self, grid):
        self.ids = FakeIds(grid)


class FakeRoot:
    def __init__(self, grid):
        self._main = FakeMain(grid)

    def get_screen(self, name):
        return self._main


class StubGuild:
    def __init__(self, files=None):
        self.files = files or {}
        self.posted = []
        self.uploads = []
        self.att_calls = []
        self.fetch_calls = []

    def post(self, channel, content, attachments=None):
        self.posted.append((channel, content, attachments))
        return {"ok": 1, "msg_id": "sent-1"}

    def post_v1(self, channel, content, attachments=None):
        self.posted.append((channel, content, attachments))
        return {"ok": 1, "msg_id": "sent-v1",
                "attachments": [{"name": "notes.txt", "mime": "text/plain",
                                 "size": 5,
                                 "path": "/file/attachments/notes.txt"}]}

    def blobs_put(self, name, mime, data, on_progress=None):
        # stand-in for the chunked uploader: one blob_id per call, bytes
        # recorded so the test can prove WHERE the payload moved
        blob_id = "bl0b" + str(len(self.uploads))
        self.uploads.append({"blob_id": blob_id, "name": name,
                             "mime": mime, "size": len(data),
                             "data": bytes(data)})
        return {"ok": 1, "blob_id": blob_id, "size": len(data),
                "path": "/file/blobs/%s" % blob_id}

    def att_get(self, path, offset=0, length=None):
        # stand-in for the chunked reader, slicing out of the fake store:
        # the offset echoes drive the client exactly like the wire does
        self.att_calls.append((path, offset))
        data = self.files.get(path) or b""
        chunk = data[offset:offset + 100000]
        return {"ok": 1, "path": path, "total": len(data),
                "offset": offset, "eof": offset + len(chunk) >= len(data),
                "data_b64": base64.b64encode(chunk).decode("ascii")}

    def avatar_get(self, user_hash):
        return {"ok": 0}

    def fetch_file(self, path):
        self.fetch_calls.append(path)
        return self.files.get(path)


class StubApp:
    """Minimal stand-in for sHadesClientApp (no RNS/guild needed)."""

    def __init__(self, kivy_client, grid, files=None):
        self.kc = kivy_client
        self.root = FakeRoot(grid)
        self.guild_api = StubGuild(files)
        self.my_hash = "a" * 32
        self.username = "me"
        self._members = {}
        self._msg_widgets = {}
        self._pending_reply = None
        self._pending_attachment = None
        self._channel_kinds = {}
        self.theme_colors = dict(kivy_client.DEFAULT_THEME_COLORS)
        self.status = []
        # real implementations, bound to this stub
        self._render_message = functools.partial(
            kivy_client.sHadesClientApp._render_message, self)
        self._fetch_attachment = functools.partial(
            kivy_client.sHadesClientApp._fetch_attachment, self)
        self._add_attachment_embed = functools.partial(
            kivy_client.sHadesClientApp._add_attachment_embed, self)
        # v0.30 rewrote _add_attachment_embed on top of the classifier and
        # the app-storage path; bind the real static method to the stub so
        # kind decisions match production, and route saves into a temp dir
        # so tests never touch the real app data dir
        self._attachment_kind = kivy_client.sHadesClientApp._attachment_kind
        self._attach_dir = tempfile.mkdtemp(prefix="shades-attach-test-")
        self._attachments_dir = lambda: self._attach_dir
        self._save_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._save_attachment_blob, self)
        # v0.31: _fetch_attachment calls _att_get_chunked for large
        # attachments; the stub must bind it or the real method resolves
        # to nothing and the chunk branch dies in the fetch try/except
        self._att_get_chunked = functools.partial(
            kivy_client.sHadesClientApp._att_get_chunked, self)
        # v0.30 non-image branch: tap-to-save rows bind through these two;
        # the handler runs only on taps, but the bind itself happens at
        # render time, so the stub must carry them
        self._download_tap = functools.partial(
            kivy_client.sHadesClientApp._download_tap, self)
        self._save_and_report = functools.partial(
            kivy_client.sHadesClientApp._save_and_report, self)
        self._record_member = functools.partial(
            kivy_client.sHadesClientApp._record_member, self)
        self._refresh_avatar = functools.partial(
            kivy_client.sHadesClientApp._refresh_avatar, self)
        self._add_loading_placeholder = functools.partial(
            kivy_client.sHadesClientApp._add_loading_placeholder, self)
        # the placeholder wording classifier (staticmethod, per tay's
        # 'loading image' for mp3's receipt)
        self._loading_placeholder_text = (
            kivy_client.sHadesClientApp._loading_placeholder_text)
        self._remove_placeholder = functools.partial(
            kivy_client.sHadesClientApp._remove_placeholder, self)
        # resume-race fix (device log 15): cache-first + retry helpers
        self._cached_attachment_blob = functools.partial(
            kivy_client.sHadesClientApp._cached_attachment_blob, self)
        self._atts_link_ok = functools.partial(
            kivy_client.sHadesClientApp._atts_link_ok, self)
        self._att_fetch_attempts_pop = functools.partial(
            kivy_client.sHadesClientApp._att_fetch_attempts_pop, self)
        # _render_message() ends by calling this to draw the reaction summary
        # line; without it the stub raises AttributeError on every render.
        self._set_row_reactions = functools.partial(
            kivy_client.sHadesClientApp._set_row_reactions, self)
        # send_message() checks the channel kind before posting.
        self._channel_kind = functools.partial(
            kivy_client.sHadesClientApp._channel_kind, self)

    def _set_status(self, text):
        self.status.append(text)

    def _channel_cid(self, name):
        return "cid-" + name


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    from client_util import ATT_SERVER
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.uix.gridlayout import GridLayout
    from kivy.lang import Builder

    # Load KV so MessageRow has embed_container id
    Builder.load_file("kv_md.kv")

    # KV rules read app.theme_colors[...], which Kivy resolves through
    # App.get_running_app(); the KV proxy also calls app.bind(). StubApp below
    # is deliberately not a Kivy App, so without this a themed widget built by
    # _render_message raises "'NoneType' object has no attribute 'bind'".
    # sHadesClientApp is an MDApp (KivyMD widgets refuse to build under a plain
    # kivy.app.App), constructs without RNS, and we pin the palette.
    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    _ThemeApp()

    # --- 1. guild-style attachment metadata (dict) ------------------------
    grid = GridLayout(cols=1)
    app = StubApp(kc, grid, files={"/file/attachments/abc.png": PNG_1PX})
    m = {"msg_id": "m1", "author_hash": "b" * 32, "username": "bob",
         "content": "here you go", "ts": time.time(),
         "attachments": [{"name": "shot.png", "mime": "image/png",
                          "size": len(PNG_1PX),
                          "path": "/file/attachments/abc.png"}]}
    kc.sHadesClientApp._render_message(app, grid, m)

    deadline = time.time() + 5
    while time.time() < deadline:
        Clock.tick()
        # Embeds are now inside MessageRow's embed_container, not direct grid children
        if len(grid.children) >= 1:
            row = grid.children[0]
            if hasattr(row.ids, "embed_container") and len(row.ids.embed_container.children) > 0:
                break
        time.sleep(0.05)
    Clock.tick()
    row = grid.children[0]
    check("guild attachment metadata is fetched and rendered",
          len(row.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row.ids.embed_container.children))

    # --- 2. local [name, mime, blob] attachment ---------------------------
    grid2 = GridLayout(cols=1)
    app2 = StubApp(kc, grid2)
    m2 = {"msg_id": "m2", "author_hash": "b" * 32, "username": "bob",
          "content": "local", "ts": time.time(),
          "attachments": [["shot.png", "image/png", PNG_1PX]]}
    kc.sHadesClientApp._render_message(app2, grid2, m2)
    Clock.tick()
    row2 = grid2.children[0]
    check("local [name, mime, blob] attachment renders an embed",
          len(row2.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row2.ids.embed_container.children))

    # --- 3. non-image attachment shows a label ----------------------------
    grid3 = GridLayout(cols=1)
    app3 = StubApp(kc, grid3)
    m3 = {"msg_id": "m3", "author_hash": "b" * 32, "username": "bob",
          "content": "file", "ts": time.time(),
          "attachments": [["notes.txt", "text/plain", b"hello"]]}
    kc.sHadesClientApp._render_message(app3, grid3, m3)
    Clock.tick()
    row3 = grid3.children[0]
    check("non-image attachment renders a label",
          len(row3.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row3.ids.embed_container.children))

    # --- 4. attachment send moves bytes through the chunked uploader ------
    # (tay's v0.30 law, receipted on the MTU failure: the blob never rides
    #  the message packet; init/chunk/finish moves it, the post carries a
    #  blob_id reference, and the render uses the server's path metadata)
    grid4 = GridLayout(cols=1)
    app4 = StubApp(kc, grid4,
                   files={"/file/attachments/notes.txt": b"hello"})
    app4.root.get_screen("main").ids.msg_input.text = "with file"
    app4._pending_attachment = ["notes.txt", "text/plain", b"hello"]
    kc.sHadesClientApp.send_message(app4)
    deadline = time.time() + 5
    while time.time() < deadline:
        Clock.tick()
        if app4.guild_api.posted:
            break
        time.sleep(0.05)
    posted = app4.guild_api.posted[0] if app4.guild_api.posted else None
    uploaded = app4.guild_api.uploads
    check("attachment send uploaded the bytes via blobs_put first",
          len(uploaded) == 1 and uploaded[0]["data"] == b"hello"
          and uploaded[0]["name"] == "notes.txt",
          repr(uploaded))
    check("send posts a blob_id reference, not inline bytes",
          posted is not None and isinstance(posted[2], list) and posted[2]
          and isinstance(posted[2][0], dict)
          and posted[2][0].get("blob_id") == (uploaded[0]["blob_id"]
                                              if uploaded else "?")
          and posted[2][0].get("name") == "notes.txt",
          repr(posted))
    Clock.tick()
    row4 = grid4.children[0]
    check("optimistically rendered message shows the attachment",
          len(row4.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row4.ids.embed_container.children))

    # --- 6. big attachments walk the chunked reader ----------------------
    # (v0.31: one receipt per slice, offset-driven, not one 15s deadline
    #  for the whole file; the full bytes land in app storage byte-equal)
    big = b"A" * 100000 + b"B" * 100000
    grid6 = GridLayout(cols=1)
    app6 = StubApp(kc, grid6,
                   files={"/file/attachments/big.txt": big})
    m6 = {"msg_id": "m-6", "author_hash": "a1", "username": "tester",
          "content": "big file", "ts": 0,
          "attachments": [{"name": "big.txt", "mime": "text/plain",
                           "size": len(big),
                           "path": "/file/attachments/big.txt"}]}
    kc.sHadesClientApp._render_message(app6, grid6, m6)
    row6 = grid6.children[0]
    deadline = time.time() + 5
    saved = None
    while time.time() < deadline:
        Clock.tick()
        if os.path.isdir(app6._attach_dir):
            for nm in os.listdir(app6._attach_dir):
                if "big" in nm:
                    saved = os.path.join(app6._attach_dir, nm)
        if (saved and len(app6.guild_api.att_calls) >= 2
                and len(row6.ids.embed_container.children) == 1):
            break
        time.sleep(0.02)
    check("large attachment walks att_get slice by slice",
          len(app6.guild_api.att_calls) >= 2
          and app6.guild_api.att_calls[0][1] == 0
          and app6.guild_api.att_calls[1][1] == 100000,
          repr(app6.guild_api.att_calls))
    check("chunked fetch assembles the full payload, byte-equal",
          bool(saved) and open(saved, "rb").read() == big,
          ("no saved file" if not saved else "mismatch"))
    check("large attachment renders its embed",
          len(row6.ids.embed_container.children) == 1,
          "%d embeds in row6" % len(row6.ids.embed_container.children))

    # tay v0.31.10 receipt: 'loading image' shown for mp3's -- the wording
    # tracks the extension now
    check("loading placeholder wording tracks the attachment kind",
          app._loading_placeholder_text("taylor track.mp3") == "[loading audio…]"
          and app._loading_placeholder_text("clip.webm") == "[loading video…]"
          and app._loading_placeholder_text("cat.jpg") == "[loading image…]"
          and app._loading_placeholder_text("notes.pdf") == "[loading file…]"
          and app._loading_placeholder_text("") == "[loading file…]",
          "mp3=%r webm=%r jpg=%r pdf=%r none=%r" % (
              app._loading_placeholder_text("taylor track.mp3"),
              app._loading_placeholder_text("clip.webm"),
              app._loading_placeholder_text("cat.jpg"),
              app._loading_placeholder_text("notes.pdf"),
              app._loading_placeholder_text("")))


    # --- 7. resume-race: cache-first + bounded retry (device log 15) ------
    # The resume replay can fire before the guild link is usable; the
    # empty fetch used to be terminal ("attachment fetch failed" above
    # the QR card). Every successful fetch already saves its blob on
    # disk, so restores consult that cache first, and a link-not-ready
    # miss retries instead of dying.
    img = bytes(range(256)) * 20  # 5120 bytes
    grid7 = GridLayout(cols=1)
    app7 = StubApp(kc, grid7, files={})  # empty hub: nothing fetchable
    p = app7._save_attachment_blob(img, "__bbs1 (2).jpg")
    check("blob of a previous fetch sits in the cache dir",
          bool(p) and os.path.getsize(p) == len(img), repr(p))
    att7 = {"name": "__bbs1 (2).jpg", "mime": "image/jpeg",
            "size": len(img), "kind": ATT_SERVER,
            "path": "/file/attachments/da035e6413534554b308c3bf.jpg"}
    hit = app7._cached_attachment_blob(att7)
    check("cache-first finds the previous blob by name and size",
          hit == img, "%s" % (len(hit) if hit else None))
    img2 = img[:-1] + bytes([255 - img[-1]])
    p2 = app7._save_attachment_blob(img2, "__bbs1 (2).jpg")
    check("ambiguous cache (two same-name same-size) refuses",
          app7._cached_attachment_blob(att7) is None)
    os.remove(p2)
    check("size mismatch refuses the cache",
          app7._cached_attachment_blob(
              dict(att7, size=len(img) + 1)) is None)
    check("single candidate without advertised size hits",
          app7._cached_attachment_blob(dict(att7, size=0)) == img)
    m7 = {"msg_id": "m-7", "author_hash": "a1", "username": "tester",
          "content": "card pic", "ts": 0, "attachments": [att7]}
    kc.sHadesClientApp._render_message(app7, grid7, m7)
    row7 = grid7.children[0]
    dl7 = time.time() + 3
    while time.time() < dl7:
        Clock.tick()
        if len(row7.ids.embed_container.children) == 1:
            break
        time.sleep(0.02)
    check("restore renders from cache with no fetch attempt",
          len(row7.ids.embed_container.children) == 1
          and app7.guild_api.att_calls == []
          and app7.guild_api.fetch_calls == [],
          "embeds=%d fetches=%d+%d" % (
              len(row7.ids.embed_container.children),
              len(app7.guild_api.fetch_calls),
              len(app7.guild_api.att_calls)))
    check("cache hit releases the retry budget",
          not getattr(app7, "_att_fetch_attempts", None))
    os.remove(p)  # cache empty again: the retry lanes exercise the network
    grid8 = GridLayout(cols=1)
    app8 = StubApp(kc, grid8, files={})
    att8 = {"name": "fresh.png", "mime": "image/png", "size": 42,
            "kind": ATT_SERVER, "path": "/file/attachments/fresh.png"}
    m8 = {"msg_id": "m-8", "author_hash": "a1", "username": "tester",
          "content": "pre-link upload", "ts": 0, "attachments": [att8]}
    kc.sHadesClientApp._render_message(app8, grid8, m8)
    row8 = grid8.children[0]
    dl8 = time.time() + 3
    retried = False
    while time.time() < dl8:
        Clock.tick()
        if any("retry" in s for s in app8.status):
            retried = True
            break
        time.sleep(0.02)
    check("pre-link empty fetch retries instead of terminal error",
          retried and not any("failed" in s for s in app8.status),
          "status=%r" % app8.status[-3:])
    # the hub becomes fetchable; the retry body (what the fired event
    # runs) re-fetches and lands the real blob
    app8.guild_api.files["/file/attachments/fresh.png"] = b"P" * 42
    app8._fetch_attachment(row8, "m-8", att8)
    dl8 = time.time() + 3
    while time.time() < dl8:
        Clock.tick()
        if len(row8.ids.embed_container.children) == 1:
            break
        time.sleep(0.02)
    check("retry renders the embed once the hub is fetchable",
          len(row8.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row8.ids.embed_container.children))
    check("resolved retry releases its budget",
          not getattr(app8, "_att_fetch_attempts", None),
          repr(getattr(app8, "_att_fetch_attempts", None)))
    # the second empty attempt IS terminal: the named error, capped at 2
    grid9 = GridLayout(cols=1)
    app9 = StubApp(kc, grid9, files={})
    att9 = {"name": "dead.png", "mime": "image/png", "size": 7,
            "kind": ATT_SERVER, "path": "/file/attachments/dead.png"}
    m9 = {"msg_id": "m-9", "author_hash": "a1", "username": "tester",
          "content": "unreachable", "ts": 0, "attachments": [att9]}
    kc.sHadesClientApp._render_message(app9, grid9, m9)
    dl9 = time.time() + 3
    while time.time() < dl9:
        Clock.tick()
        if any("retry" in s for s in app9.status):
            break
        time.sleep(0.02)
    # budget already at 1 (the render attempt); the fired retry hits the cap
    app9._fetch_attachment(grid9.children[0], "m-9", att9)
    dl9 = time.time() + 3
    while time.time() < dl9:
        Clock.tick()
        if any("attachment fetch failed: dead.png" in s for s in app9.status):
            break
        time.sleep(0.02)
    check("second empty fetch is terminal with the named error",
          any("attachment fetch failed: dead.png" in s for s in app9.status),
          repr(app9.status[-2:]))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
