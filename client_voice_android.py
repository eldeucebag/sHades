"""Android voice device I/O (build-order item 7).

Same contracts as client_voice.MicSource/SpeakerSink, on jnius:

- AndroidMic.poll() -> one codec-rate frame (960 s16 mono samples,
  1920 bytes) per call, drained from an AudioRecord callback thread.
- AndroidSpeaker(pcm) takes codec-rate PCM; one thread writes the
  AudioTrack, and a silent buffer keeps the track rolling while the
  player loop has nothing.

Everything here raises VoiceDeviceError at construction (with the
jnius reason) when the platform lacks the classes: the panel treats
that exactly like the desktop's "no device" seat (listen-only, named
in the status line).
"""
import collections
import threading
import time

import voice_codec

# one error identity across the device layers: the panel catches the
# core's VoiceDeviceError, so a local class here would escape the
# listen-only handler (receipt: device log 9 - "AudioRecord init
# failed" escaped as an exception of the local class, the setup aborted
# after the lane registered, and every re-join died with RNS's
# "already registered destination")
from client_voice_core import VoiceDeviceError


FRAME_BYTES = voice_codec.FRAME_BYTES        # 1920 (960 samples, s16)
FRAME_SAMPLES = voice_codec.FRAME_SAMPLES


def _jni():
    """The jnius imports, or VoiceDeviceError with the reason."""
    try:
        from jnius import autoclass
        return autoclass
    except Exception as e:
        raise VoiceDeviceError("jnius unavailable: %r" % e)


# jnius-free constant base: the values Android documents for these
# (AudioFormat/Managers are stable); the real classes override the
# symbolic fields when the platform probe answers.
_TEST_BASE = {
    "ENCODING_PCM_16BIT": 2,          # AudioFormat.ENCODING_PCM_16BIT
    "CHANNEL_IN_MONO": 16,            # AudioFormat.CHANNEL_IN_MONO
    "CHANNEL_OUT_MONO": 4,            # AudioFormat.CHANNEL_OUT_MONO
    "STREAM_MUSIC": 3,                # AudioManager.STREAM_MUSIC
    "MODE_STREAM": 1,                 # AudioTrack.MODE_STREAM
    "STATE_UNINITIALIZED": 0,
}


def _audio_classes(test_overrides=None):
    """AudioRecord/AudioTrack/AudioFormat/AndroidConstant pairs, with
    the argument the recorder needs resolved (VOICE_COMMUNICATION gets
    the platform's echo suppression where the build offers it). With
    test_overrides, a jnius-free run stands in fakes for the classes
    and rides the documented constants above."""
    if test_overrides:
        base = dict(_TEST_BASE)
        base["source"] = 0            # MediaRecorder.AudioSource.DEFAULT
        base.update(test_overrides)
        base["mocked"] = True
        return base
    autoclass = _jni()
    fmt = autoclass("android.media.AudioFormat")
    media_rec = autoclass("android.media.MediaRecorder$AudioSource")
    try:
        rec_src = media_rec.VOICE_COMMUNICATION
    except Exception:
        rec_src = media_rec.MIC
    return {
        "autoclass": autoclass,
        "AudioRecord": autoclass("android.media.AudioRecord"),
        "AudioTrack": autoclass("android.media.AudioTrack"),
        "AudioFormat": fmt,
        "source": rec_src,
        "ENCODING_PCM_16BIT": fmt.ENCODING_PCM_16BIT,
        "CHANNEL_IN_MONO": fmt.CHANNEL_IN_MONO,
        "CHANNEL_OUT_MONO": fmt.CHANNEL_OUT_MONO,
        # AudioTrack's int constructor (simplest jnius interop) takes
        # streamType first and mode last: STREAM_MUSIC != MODE_STREAM
        "STREAM_MUSIC": 3,
        "MODE_STREAM": 1,
        "STATE_UNINITIALIZED": 0,
        "mocked": False,
    }


def _mic_permission_missing():
    """True when the RECORD_AUDIO runtime grant is absent; fires the
    grant dialog when it is, then re-checks: p4a's helper resolves the
    dialog from the calling thread without a re-join (device log 13 -
    join -> dialog -> grant landed, but the join still owed a second
    tap; now a grant inside the dialog goes live immediately). A denial
    or a permanent block keeps this join listen-only with the named
    reason; the next one reaps any later grant."""
    try:
        from android.permissions import check_permission, Permission
    except Exception:
        return False
    try:
        if check_permission(Permission.RECORD_AUDIO):
            return False
    except Exception:
        return False
    try:
        from android.permissions import request_permissions
        request_permissions([Permission.RECORD_AUDIO])
    except Exception:
        pass
    try:
        if check_permission(Permission.RECORD_AUDIO):
            return False
    except Exception:
        pass
    return True


def min_buffer(rec_cls, fmt_cls, rate, encoding, channel):
    return rec_cls.getMinBufferSize(rate, channel, encoding)


class AndroidMic:
    """AudioRecord-backed mic with ring + poll, one frame per poll."""

    IDLE = None

    def __init__(self, jnius_overrides=None, rate=voice_codec.SAMPLE_RATE,
                 channels=1):
        self.rate, self.channels = rate, channels
        cls = _audio_classes(test_overrides=jnius_overrides)
        self._perm_missing = (False if cls.get("mocked")
                              else _mic_permission_missing())
        buf_min = cls["AudioRecord"].getMinBufferSize(
            rate, cls["CHANNEL_IN_MONO"], cls["ENCODING_PCM_16BIT"])
        if buf_min <= 0:
            raise VoiceDeviceError(
                "AudioRecord.getMinBufferSize rejected %d Hz mono" % rate)
        self._ring = collections.deque()
        self._lock = threading.Lock()
        self._drop = 0
        # the real jnius object (or the injected fake)
        self._rec = cls["AudioRecord"].__call__(
            cls["source"], rate, cls["CHANNEL_IN_MONO"],
            cls["ENCODING_PCM_16BIT"], max(buf_min, FRAME_BYTES * 10))
        state = getattr(self._rec, "getState", None)
        if state is not None and state() == cls["STATE_UNINITIALIZED"]:
            if not cls.get("mocked") and self._perm_missing:
                raise VoiceDeviceError(
                    "AudioRecord init failed (RECORD_AUDIO permission "
                    "not granted; allow the mic and re-join)")
            raise VoiceDeviceError("AudioRecord init failed")
        self._reader = threading.Thread(target=self._loop, daemon=True)
        self.frames_read = 0
        self._stop = threading.Event()
        self._buf = bytearray(FRAME_BYTES)     # filled in place by read()
        self._rec.startRecording()
        self._reader.start()

    def _loop(self):
        read = self._rec.read
        buf = self._buf
        while not self._stop.is_set():
            # jnius: AudioRecord.read's byte[] overload is the 3-arg
            # read(byte[] buf, int offsetInBytes, int sizeInBytes);
            # the 2-arg call matches no Java surface (device log 12:
            # jnius.JavaException - available: ([BII)I for bytes)
            n = read(buf, 0, FRAME_BYTES)
            if not isinstance(n, int) or n != FRAME_BYTES:
                continue
            with self._lock:
                if len(self._ring) > 50:
                    self._ring.popleft()
                    self._drop += 1
                self._ring.append(bytes(buf))

    def poll(self):
        with self._lock:
            if self._ring:
                self.frames_read += 1
                return self._ring.popleft()
        return None

    def close(self):
        self._stop.set()
        try:
            self._reader.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._rec.stop()
            self._rec.release()
        except Exception:
            pass


class AndroidSpeaker:
    """AudioTrack-backed sink: queued PCM drained by one thread."""

    def __init__(self, jnius_overrides=None, rate=voice_codec.SAMPLE_RATE,
                 channels=1):
        self.rate, self.channels = rate, channels
        cls = _audio_classes(test_overrides=jnius_overrides)
        buf_min = cls["AudioTrack"].getMinBufferSize(
            rate, cls["CHANNEL_OUT_MONO"], cls["ENCODING_PCM_16BIT"])
        if buf_min <= 0:
            raise VoiceDeviceError(
                "AudioTrack.getMinBufferSize rejected %d Hz mono" % rate)
        self._track = cls["AudioTrack"].__call__(
            cls["STREAM_MUSIC"], rate, cls["CHANNEL_OUT_MONO"],
            cls["ENCODING_PCM_16BIT"], max(buf_min, FRAME_BYTES * 20),
            cls["MODE_STREAM"])
        self._track.play()
        self._q = collections.deque()
        self._lock = threading.Lock()
        self._played_frames = 0
        self._bytes_in = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def __call__(self, pcm):
        if not pcm:
            return
        self._bytes_in += len(pcm)
        with self._lock:
            self._q.append(bytes(pcm))

    def _drain(self):
        write = self._track.write
        while not self._stop.is_set():
            with self._lock:
                pcm = self._q.popleft() if self._q else None
            if pcm is None:
                # silence keeps the track rolling: the player loop's
                # PLC frames and these both land as audio writes
                write(b"\x00" * FRAME_BYTES, 0, FRAME_BYTES)
                time.sleep(0.01)
                continue
            self._played_frames += len(pcm) // FRAME_BYTES
            write(pcm, 0, len(pcm))

    def stats(self):
        return {"bytes_in": self._bytes_in,
                "frames_played": self._played_frames}

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._track.stop()
            self._track.release()
        except Exception:
            pass
