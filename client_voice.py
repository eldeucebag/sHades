"""Desktop audio device I/O for the voice engine (build-order item 5).

Desktop layer only: MicSource and SpeakerSink over PyAudio. The
transport core (VoiceLane, RNSLinkShell, bind, VoiceDeviceError, the
frame geometry) lives in client_voice_core -- the panel imports that on
every platform and picks this module's device classes on desktop only,
so a device boot never needs pyaudio (receipt: the pre-split join path
failed on the device with "No module named 'pyaudio'" before any
mic/sink choice, because the panel reached here for the RNS lane).

Both pieces are daemon-like, both called from the engine's own loops:

- MicSource: a ring buffer filled by PyAudio's stream callback and
  drained 20ms-frame-at-a-time by the engine's pacer (10Hz x
  PACKET_FRAMES calls/second, one frame per call).
- SpeakerSink: called from the engine's player loop with decoded PCM;
  a drain thread keeps the device stream fed so the player loop never
  blocks on a C call.

No device: every constructor raises VoiceDeviceError (from
client_voice_core) with the reason, so the client can present
"voice unavailable" instead of half-wired.
"""
import collections
import threading
import time

import voice_codec
import pyaudio

# transport core moved to client_voice_core; re-exported here so the
# desktop device gate keeps one import surface
from client_voice_core import (FRAME_BYTES, FRAMES_PER_CHUNK,
                               VoiceDeviceError)
from client_voice_core import FRAME_MS, RNSLinkShell, VoiceLane, bind  # noqa: F401


class MicSource:
    """One 20ms frame (640x2B per poll()); captures continuously."""

    def __init__(self, device=None, rate=voice_codec.SAMPLE_RATE,
                 channels=voice_codec.CHANNELS):
        self.pa = pyaudio.PyAudio()
        spec = _pick(self.pa, want_output=False, device=device)
        self._ring = collections.deque()   # 20ms frames as bytes
        self._ring_lock = threading.Lock()
        self._drop = 0
        self._stream = self.pa.open(
            format=pyaudio.paInt16, channels=channels, rate=rate,
            input=True, output=False, input_device_index=spec,
            frames_per_buffer=FRAMES_PER_CHUNK,
            stream_callback=self._cb)
        self.rate, self.channels = rate, channels
        self.frames_read = 0

    def _cb(self, in_data, frame_count, time_info, status_flags):
        with self._ring_lock:
            if len(self._ring) > 50:
                # the ring is bounded; a stalled pacer loses the oldest
                # frame, the newest always survive
                self._ring.popleft()
                self._drop += 1
            self._ring.append(in_data)
        return (None, pyaudio.paContinue)

    def poll(self):
        with self._ring_lock:
            if self._ring:
                self.frames_read += 1
                return self._ring.popleft()
        return None

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self.pa.terminate()


class SpeakerSink:
    """Queues decoded PCM; one drain thread writes the device stream.
    __call__(pcm) is the engine sink callback."""

    def __init__(self, device=None, rate=voice_codec.SAMPLE_RATE,
                 channels=voice_codec.CHANNELS):
        self.pa = pyaudio.PyAudio()
        spec = _pick(self.pa, want_output=True, device=device)
        self._stream = self.pa.open(
            format=pyaudio.paInt16, channels=channels, rate=rate,
            input=False, output=True, output_device_index=spec,
            frames_per_buffer=FRAMES_PER_CHUNK)
        self.rate, self.channels = rate, channels
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
        while not self._stop.is_set():
            with self._lock:
                pcm = self._q.popleft() if self._q else None
            if pcm is None:
                # keep the device stream rolling (silence = PLC output)
                self._stream.write(b"\x00" * FRAME_BYTES,
                                   exception_on_underflow=False)
                time.sleep(0.01)
                continue
            self._played_frames += len(pcm) // FRAME_BYTES
            self._stream.write(pcm, exception_on_underflow=False)

    def stats(self):
        return {"bytes_in": self._bytes_in,
                "frames_played": self._played_frames}

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self.pa.terminate()


def _pick(pa, want_output, device=None):
    if device is not None:
        info = pa.get_device_info_by_index(device)
        if (info["maxOutputChannels"] if want_output
                else info["maxInputChannels"]) < 1:
            raise VoiceDeviceError(
                "device %d has no %s channels"
                % (device, "output" if want_output else "input"))
        return device
    # default first: PyAudio resolves the system default either way
    try:
        idx = (pyaudio.PyAudio.get_default_output_device_info(pa)
               if want_output else
               pyaudio.PyAudio.get_default_input_device_info(pa))["index"]
        return int(idx)
    except Exception as e:
        raise VoiceDeviceError("no usable %s device: %r"
                               % ("speaker" if want_output else "microphone", e))
