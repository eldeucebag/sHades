"""Voice rows on the rail + the join panel (build-order item 6).

Mixin for the client app. Overrides StreamMixin's on_voice_state stub.
The room roster arrives as a voice_state push on the room channel's
stream (app_stream already routes it); this mixin:

- keeps the rail's voice rows current (count + names under each
  voice-kind channel button),
- feeds the joined room's roster to the VoiceEngine (its dialer picks
  up peer voice destinations off it),
- joins: opens this session's RNS voice listener, registers with the
  hub (the join response seeds the roster), binds mic/sink, wires the
  lane; leaves: tears the lot down in reverse.

The engine and lane are one per app, so a channel change never
re-opens the audio devices.
"""
import threading

from client_util import dlog

VOICE_APP_NAME = "shades"
VOICE_ASPECT = "voice"


class VoicePanelMixin:
    """Voice rows, join panel, engine/lane ownership."""

    # ---- engine + lane state -------------------------------------------

    def _voice_state_init(self):
        self._voice_rooms = {}          # cid -> [member dicts]
        self._voice_engine = None
        self._voice_lane = None
        self._voice_room_cid = None
        self._voice_muted = False
        self._voice_rows = {}           # cid -> VoiceRow
        self._voice_lock = threading.RLock()

    # ---- push path ------------------------------------------------------

    def on_voice_state(self, cid, members):
        """StreamMixin stub override. Rostered members for one room."""
        members = [m for m in (members or []) if isinstance(m, dict)]
        with self._voice_lock:
            if members:
                self._voice_rooms[cid] = members
            else:
                self._voice_rooms.pop(cid, None)
        engine = getattr(self, "_voice_engine", None)
        if engine is not None and cid == getattr(self, "_voice_room_cid",
                                                 None):
            # the engine's dialer consumes roster rows post-join; the
            # push lands on an RNS thread, engine calls are threadsafe
            engine.on_roster(cid, members)
        try:
            Clock.schedule_once(
                lambda dt: self._voice_rail_refresh(cid), 0)
        except Exception:
            pass

    # ---- rail (voice rows) ---------------------------------------------

    def _voice_rail_refresh(self, cid=None):
        """Update every voice row present in the sidebar."""
        rows = getattr(self, "_voice_rows", None)
        if rows is None:
            return
        try:
            self.root.get_screen("main")
        except Exception:
            return
        for cid, w in list(rows.items()):
            w.render(self._voice_rooms.get(cid),
                     joined=cid == self._voice_room_cid,
                     muted=self._voice_muted)

    def _voice_row_for(self, name, cid):
        """The sidebar's voice row widget for a channel; built lazily."""
        if cid in self._voice_rows:
            return self._voice_rows[cid]
        from client_widgets import VoiceRow
        row = VoiceRow(channel_name=name)
        row.bind_action(self._voice_tap_join, self._voice_tap_leave,
                        self._voice_tap_toggle_mute)
        self._voice_rows[cid] = row
        self._voice_rail_refresh(cid)
        return row

    # ---- actions --------------------------------------------------------

    def _voice_tap_join(self, name):
        cid = self._channels.get(name)
        if not cid:
            self._set_status("cannot resolve #%s on the guild" % name)
            return
        self._voice_join(cid)

    def _voice_tap_leave(self, name=None):
        threading.Thread(target=self._voice_leave_work, daemon=True).start()

        def ui(dt):
            self._voice_rail_refresh()
        Clock.schedule_once(ui, 0)

    def _voice_tap_toggle_mute(self, name=None):
        engine = getattr(self, "_voice_engine", None)
        if engine is None:
            return
        self._voice_muted = not self._voice_muted
        engine.set_muted(self._voice_muted)
        self._set_status("microphone %s" %
                         ("muted" if self._voice_muted else "open"))
        Clock.schedule_once(lambda dt: self._voice_rail_refresh(), 0)

    # ---- engine + lane lifecycle ---------------------------------------

    def _voice_ensure_engine(self):
        """One engine + lane per app session; created on first join."""
        if self._voice_engine is not None:
            return True
        user_hash = getattr(self, "my_user_hash", "") or ""
        if not user_hash:
            print("voice join: whoami has not answered yet; retry later")
            return False
        try:
            import client_voice_core as core
            from app_voice import VoiceEngine

            class HubGate:
                """api metadata + relay publish over the guild link."""

                def __init__(self, api):
                    self.api = api

                def join(self, cid, dest, muted):
                    return self.api.voice_join(cid, dest, muted)

                def leave(self):
                    return self.api.voice_leave()

                def voice_publish(self, cid, body):
                    return self.api.voice_publish(cid, body)

            engine = VoiceEngine(viewer=user_hash, hub=HubGate(self.guild_api))
            my_ident = RNS.Identity.from_file(self._identity_file())
            lane = core.VoiceLane(my_ident)
            my_dest = lane.open_listener(engine)
            engine.set_dest(my_dest)
            engine.set_link_factory(lane.dial_factory(engine))
            mic = speaker = None
            # item 7: AudioRecord/AudioTrack on the platform; the
            # contracts are identical, so the engine never changes
            try:
                if is_android():
                    import client_voice_android as dev
                    mic = dev.AndroidMic()
                    speaker = dev.AndroidSpeaker()
                else:
                    import client_voice as desk
                    mic = desk.MicSource()
                    speaker = desk.SpeakerSink()
            except core.VoiceDeviceError as e:
                print("voice devices unavailable (listen-only): %r" % e)
            core.bind(engine, mic=mic, speaker=speaker)
            self._voice_engine = engine
            self._voice_lane = lane
            self._voice_mic = mic
            self._voice_speaker = speaker
            return True
        except Exception as e:
            print("voice engine setup failed: %r" % e)
            return False

    def _voice_join(self, cid):
        if not self._voice_ensure_engine():
            self._set_status("voice: engine unavailable (try again)")
            return
        res = self._voice_engine.join(cid, muted=self._voice_muted)
        if not res or not res.get("ok"):
            self._set_status("voice join refused: %s" %
                             (res or {}).get("error", "?"))
            return
        self._voice_room_cid = cid
        members = res.get("members") or []
        # the join response is the room's live roster; push covers later
        self._voice_rooms[cid] = members
        self._voice_engine.on_roster(cid, members)
        self._set_status("voice: joined")
        Clock.schedule_once(lambda dt: self._voice_rail_refresh(cid), 0)

    def _voice_leave_work(self):
        engine = getattr(self, "_voice_engine", None)
        if engine is None:
            return
        try:
            r = engine.leave()
            dlog("voice leave: %r" % r)
        except Exception as e:
            print("voice leave failed: %r" % e)
        self._voice_room_cid = None

    def on_stop(self):
        """App teardown: close the lane + devices."""
        engine = getattr(self, "_voice_engine", None)
        lane = getattr(self, "_voice_lane", None)
        try:
            if engine is not None:
                engine.leave()
        except Exception:
            pass
        try:
            if lane is not None:
                lane.close_listener()
        except Exception:
            pass
        for dev in ("_voice_mic", "_voice_speaker"):
            d = getattr(self, dev, None)
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass


try:
    from kivy.clock import Clock
except Exception:            # pragma: no cover - lint without kivy
    Clock = None
try:
    import RNS
    from RNS.vendor.platformutils import is_android
except Exception:            # pragma: no cover
    RNS = None

    def is_android():            # no RNS here: assume not Android
        return False
