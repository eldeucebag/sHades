#!/usr/bin/env python3
"""Shape gate for client_voice_android (build-order item 7).

Runs anywhere: the jnius boundary is injected as fakes shaped like
Android's own classes (read(byte[], int) fills in place, three-arg
write). Pins:
  - AndroidMic.poll() hands codec-rate frames (1920 B) at one frame
    per call, ring bounded,
  - the reader loop's in-place bytearray fill + count contract,
  - AndroidSpeaker drains the queue into write(byte[], off, len),
  - construction without a jnius platform raises VoiceDeviceError
    naming the cause on the desktop engine's platform switch.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

PASSES, FAILS = [], []


def check(name, cond, extra=""):
    (PASSES if cond else FAILS).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""), flush=True)


import voice_codec

FRAME_BYTES = voice_codec.FRAME_BYTES       # 1920


class _FakeAudioRecord:
    """read(buf, n) fills buf in place, returns n. Java-faithful."""

    @staticmethod
    def getMinBufferSize(rate, channel, fmt):
        return 4096

    def __init__(self, source, rate, channel, encoding, buf):
        self.source, self.rate, self.buf_bytes = source, rate, buf
        self.state = 1                      # RECORDSTATE_RECORDING
        self.frames_made = 0
        self._phase = 0

    def getState(self):
        return self.state                   # >0: UNINITIALIZED is 0

    def startRecording(self):
        self.state = 3

    def read(self, buf, offset_in_bytes, size):
        # Android's byte[] overload is read(byte[], int, int) - the
        # 2-arg call matches no Java surface (device log 12, jnius):
        # the real device needs offset 0 explicitly.
        if size != FRAME_BYTES:
            return -1                       # wrong overload use: loud
        if offset_in_bytes != 0:
            return -2
        # 960 s16 mono frame; a quiet sine stands in for capture
        import math
        self._phase += voice_codec.FRAME_SAMPLES
        vals = [int(300 * math.sin(2 * math.pi * 440 * i / 48000))
                for i in range(voice_codec.FRAME_SAMPLES)]
        for idx, v in enumerate(vals):
            buf[2 * idx] = v & 0xFF
            buf[2 * idx + 1] = (v >> 8) & 0xFF
        self.frames_made += 1
        return size

    def stop(self):
        self.state = 1

    def release(self):
        self.state = 0


class _FakeAudioTrack:
    @staticmethod
    def getMinBufferSize(rate, channel, fmt):
        return 4096

    def __init__(self, stream_type, rate, channel, encoding, buf, mode):
        self.rate, self.stream_type, self.mode = rate, stream_type, mode
        self.written = []
        self.state = 1

    def play(self):
        self.state = 3

    def write(self, data, offset, size):
        self.written.append(bytes(data[offset:offset + size]))
        return size

    def stop(self):
        self.state = 1

    def release(self):
        self.state = 0


FAKES = {
    "AudioRecord": _FakeAudioRecord,
    "AudioTrack": _FakeAudioTrack,
}


def mic_shape():
    import client_voice_android as dev
    mic = dev.AndroidMic(jnius_overrides=FAKES)
    got = []
    for _ in range(40):
        f = mic.poll()
        if f:
            got.append(f)
    check("mic delivers codec-rate frames",
          got and size_ok(got), "frames=%d" % len(got))
    check("one frame per poll (contract)",
          len(got) >= 2 and mic.frames_read == len(got),
          "read=%d" % mic.frames_read)
    if got:
        codec = voice_codec.VoiceCodec()
        try:
            codec.encode(got[-1])
            check("fake mic frame encodes at codec rate", True)
        except Exception as e:
            check("fake mic frame encodes at codec rate", False, repr(e))
    mic.close()
    return mic


def size_ok(frames):
    return all(len(f) == FRAME_BYTES for f in frames)


def speaker_shape():
    import client_voice_android as dev
    sink = dev.AndroidSpeaker(jnius_overrides=FAKES)
    pcm = b"\x01\x02" * (FRAME_BYTES // 2)
    for _ in range(6):
        sink(pcm)
    deadline = time.time() + 2.0
    while time.time() < deadline and sink._played_frames < 6:
        time.sleep(0.05)
    wrote = sum(1 for w in sink._track.written if len(w) == FRAME_BYTES)
    check("speaker drains queued pcm to write(buf, off, len)", wrote >= 6,
          "wrote=%d frames_played=%d" %
          (wrote, sink._played_frames))
    check("write sizes are codec-rate frames", wrote >= 1)
    stats = sink.stats()
    check("stats shape", stats["bytes_in"] == 6 * len(pcm)
          and stats["frames_played"] >= 6, stats)
    sink.close()
    return sink


def stream_type_semantics():
    """AudioTrack's int ctor: STREAM_MUSIC (3) first, MODE_STREAM (1)
    last. The first cut passed MODE_STREAM as the stream type, which
    Android reads as STREAM_SYSTEM."""
    import client_voice_android as dev
    sink = dev.AndroidSpeaker(jnius_overrides=FAKES)
    check("track stream type is STREAM_MUSIC",
          sink._track.stream_type == 3 and sink._track.mode == 1,
          "type=%d mode=%d" % (sink._track.stream_type, sink._track.mode))
    sink.close()


def record_source_semantics():
    """The recorder's source is VOICE_COMMUNICATION where the build
    provides it; the fake mirrors the module's resolution."""
    import client_voice_android as dev
    mic = dev.AndroidMic(jnius_overrides=FAKES)
    check("mic recorder constructed", mic._rec.state in (1, 3), mic._rec.state)
    mic.close()


def desktop_no_jnius():
    """Off-platform: _jni() raises VoiceDeviceError naming the cause."""
    import client_voice_android as dev
    try:
        dev._audio_classes()
        check("desktop without jnius raises", False, "no raise")
    except dev.VoiceDeviceError as e:
        check("desktop without jnius raises", "jnius" in str(e), str(e))
    except Exception as e:
        check("desktop without jnius raises", False, repr(e))


def permission_reap_lane():
    """Device log 13: a grant completing inside the dialog lets the
    same join proceed - check, request, re-check - instead of owing a
    second tap. Stubs android.permissions in sys.modules for the lane."""
    import sys
    import types
    import client_voice_android as dev
    calls = {"n": 0}
    reqs = []

    perm = types.ModuleType("android.permissions")

    class P:
        RECORD_AUDIO = "android.permission.RECORD_AUDIO"

    def perm_check(name):
        calls["n"] += 1
        # order: 1 pre-dialog (missing), 2 post-dialog (granted)
        return calls["n"] >= 2

    def request_permissions(whats):
        reqs.append(list(whats))
        return {P.RECORD_AUDIO: True}

    perm.check_permission = perm_check
    perm.Permission = P
    perm.request_permissions = request_permissions
    android_pkg = types.ModuleType("android")
    android_pkg.permissions = perm
    saved = {k: sys.modules.get(k) for k in ("android", "android.permissions")}
    sys.modules["android"] = android_pkg
    sys.modules["android.permissions"] = perm
    try:
        ok = dev._mic_permission_missing() is False
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    check("grant mid-join reaps (same join goes live)",
          ok and reqs and reqs[0][0] == P.RECORD_AUDIO, reqs)


def main():
    print("frame geometry: FRAME_BYTES=%d" % FRAME_BYTES)
    desktop_no_jnius()
    permission_reap_lane()
    mic_shape()
    speaker_shape()
    stream_type_semantics()
    record_source_semantics()
    print("RESULT: %d pass, %d fail" % (len(PASSES), len(FAILS)))
    sys.exit(0 if not FAILS else 1)


if __name__ == "__main__":
    main()
