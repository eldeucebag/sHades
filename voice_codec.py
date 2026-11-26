
# -*- coding: utf-8 -*-
#
# VoiceCodec: opus encode/decode for voice channels (tay, 2026-11-23).
#
# Design receipt: the codec is the only NEW dependency in the voice
# build. Everything else rides proven transports (guild links, the
# push path). This module is deliberately tiny: locate libopus, wrap
# opus (48 kHz mono, 20 ms frames, VoIP mode), expose encode/decode.
#
# Frame budget: 20 ms at 48 kHz = 960 samples. At 24 kbps VoIP an opus
# frame is roughly 60-80 bytes; 5 frames per 100 ms outbound packet
# stays inside the RNS packet budget with room for the msgpack wrap.

import ctypes
import ctypes.util
import math
import os
import struct
import sys

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960          # 20 ms at 48 kHz
CHANNELS = 1
FRAME_BYTES = FRAME_SAMPLES * 2   # 16-bit mono PCM per frame
DEFAULT_BITRATE = 24000
# libopus constants (ctypes binding): OPUS_APPLICATION_VOIP 2048,
# OPUS_SET_BITRATE_REQUEST 4002
_OPUS_APP_VOIP = 2048
_OPUS_SET_BITRATE = 4002


class VoiceCodecUnavailable(Exception):
    """libopus could not be located; voice stays off, everything else runs."""


ANDROID_ENV = ("ANDROID_ARGUMENT" in os.environ
               or os.environ.get("KIVY_BUILD_ANDROID") == "1")


def _android_native_lib_dir():
    """This app's nativeLibraryDir, or None off the platform.

    Android has no ldconfig: ctypes.util.find_library cannot see the
    .so files p4a shipped inside the APK's lib dir. PythonActivity
    (org.kivy.android) carries the running context, whose
    applicationInfo.nativeLibraryDir is the directory the process's
    LD_LIBRARY_PATH already searches at boot.
    """
    if not ANDROID_ENV:
        return None
    try:
        from jnius import autoclass
        pyact = autoclass("org.kivy.android.PythonActivity")
        info = pyact.mActivity.getApplicationInfo()
        return getattr(info, "nativeLibraryDir", None)
    except Exception:
        return None


def _opus_lib_path():
    """Find libopus on this platform.

    Priority: HADES_OPUS_LIB (explicit), then ctypes.util.find_library,
    then the SDL2 runtime dir next to the active prefix (the desktop
    build venv ships libopus-0.dll with libSDL2), then the exe dir
    (the PyInstaller onefile layout), then PATH-probed mingw names.
    Returns an absolute path, or None to let find_library try alone.
    """
    env_path = os.environ.get("HADES_OPUS_LIB")
    if env_path and os.path.isfile(env_path):
        return env_path
    found = ctypes.util.find_library("opus")
    if found:
        return found
    candidates = []
    prefix = getattr(sys, "prefix", "")
    candidates.append(os.path.join(prefix, "share", "sdl2", "bin"))
    if hasattr(sys, "frozen") and sys.frozen is True:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(exe_dir)
        candidates.append(os.path.join(exe_dir, "share", "sdl2", "bin"))
    names = ["libopus-0.dll", "libopus.so.0", "libopus.0.dylib",
             "libopus-0.so", "libopus.dll"]
    app_dir = _android_native_lib_dir()
    if app_dir:
        candidates.append(app_dir)
        if "libopus.so" not in names:
            names = names + ["libopus.so"]
    for d in candidates:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


class VoiceCodec(object):
    """opus encode/decode sized for one-way speaker fan-out.

    decode(None) hands back the PLC frame so a dropped packet heals the
    timeline instead of stalling it; nothing is ever retransmitted.
    """

    def __init__(self, bitrate=DEFAULT_BITRATE):
        self.bitrate = bitrate
        path = _opus_lib_path()
        if not path:
            raise VoiceCodecUnavailable(
                "libopus not found (set HADES_OPUS_LIB to the dll path)")
        # pure-ctypes binding, no pure-python opuslib dependency: the APK
        # ships only the opus C recipe (opuslib + cffi are not in the
        # device package list), so opuslib's import raised "the binding
        # failed to import" on every device join even with libopus.so
        # sitting in nativeLibraryDir (receipt: device log 8, build
        # Nov 25 19:17). This binding loads the .so directly.
        try:
            lib = ctypes.CDLL(path)
        except OSError as e:
            raise VoiceCodecUnavailable(
                "libopus at %s failed to load: %r" % (path, e))
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decode.restype = ctypes.c_int
        # opus_encoder_ctl is variadic; fixed 3-arg shape fits every
        # call this binding makes (one int request + one int value)
        lib.opus_encoder_ctl.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.opus_encoder_ctl.restype = ctypes.c_int
        err = ctypes.c_int(0)
        enc = lib.opus_encoder_create(
            SAMPLE_RATE, CHANNELS, _OPUS_APP_VOIP, ctypes.byref(err))
        if not enc or err.value != 0:
            raise VoiceCodecUnavailable(
                "opus_encoder_create failed (err %d)" % err.value)
        # speech-optimised VoIP application (2048); set the bitrate the
        # same way opuslib's Encoder.bitrate property did
        if lib.opus_encoder_ctl(enc, _OPUS_SET_BITRATE, int(bitrate)) != 0:
            lib.opus_encoder_destroy(enc)
            raise VoiceCodecUnavailable("opus_encoder_ctl bitrate failed")
        err = ctypes.c_int(0)
        dec = lib.opus_decoder_create(
            SAMPLE_RATE, CHANNELS, ctypes.byref(err))
        if not dec or err.value != 0:
            lib.opus_encoder_destroy(enc)
            raise VoiceCodecUnavailable(
                "opus_decoder_create failed (err %d)" % err.value)
        self._opus = lib
        self._enc = enc
        self._dec = dec
        self._enc_out = ctypes.create_string_buffer(4000)
        self._dec_out = (ctypes.c_int16 * FRAME_SAMPLES)()

    def encode(self, pcm):
        """One 20 ms frame: 960 int16 mono samples -> opus bytes."""
        if len(pcm) != FRAME_BYTES:
            raise ValueError("pcm frame must be %d bytes" % FRAME_BYTES)
        n = self._opus.opus_encode(
            self._enc, pcm, FRAME_SAMPLES,
            ctypes.addressof(self._enc_out), len(self._enc_out))
        if n < 0:
            raise RuntimeError("opus_encode failed (%d)" % n)
        return bytes(self._enc_out[:n])

    def decode(self, data):
        """opus bytes (or None for PLC) -> 960-sample int16 mono frame.

        None goes to opus_decode with a NULL input: packet loss
        concealment, one mono frame of comfort noise, never a stall.
        Dropped packets surface as one None per missing frame; the
        jitter layer calls this per frame.
        """
        if data is None:
            n = self._opus.opus_decode(
                self._dec, None, 0, self._dec_out, FRAME_SAMPLES, 0)
        else:
            n = self._opus.opus_decode(
                self._dec, bytes(bytearray(data)), len(data), self._dec_out,
                FRAME_SAMPLES, 0)
        if n < 0:
            raise RuntimeError("opus_decode failed (%d)" % n)
        return bytes(self._dec_out)


def encode_packet(codec, frames):
    """Pack N codec frames into one packet body: seq + lengths + frames.

    The receiver splits on declared lengths, so a packet survives any
    downstream transport that preserves byte order (RNS links do).
    """
    out = [len(frames).to_bytes(2, "big")]
    for f in frames:
        out.append(len(f).to_bytes(4, "big"))
        out.append(f)
    return b"".join(out)


def decode_packet(body):
    """Inverse of encode_packet: -> list of frame bytes."""
    if len(body) < 2:
        raise ValueError("short packet")
    count = int.from_bytes(body[:2], "big")
    pos = 2
    frames = []
    for _ in range(count):
        ln = int.from_bytes(body[pos:pos + 4], "big")
        pos += 4
        frames.append(body[pos:pos + ln])
        pos += ln
    if pos != len(body):
        raise ValueError("trailing bytes in packet")
    return frames


def parse_pcm_wav(path):
    """Minimal 16-bit PCM mono/stereo WAV reader for the bench."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF WAVE file")
    pos = 12
    fmt = None
    body = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        ln = int.from_bytes(data[pos + 4:pos + 8], "little")
        chunk = data[pos + 8:pos + 8 + ln]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", chunk[:16])
        elif cid == b"data":
            body = chunk
        pos += 8 + ln + (ln & 1)
    if not fmt or body is None:
        raise ValueError("wav missing fmt/data chunks")
    afmt, chans, rate, _br, _ba, bits = fmt
    if afmt != 1 or bits != 16:
        raise ValueError("need 16-bit PCM wav")
    if chans == 2:
        s = struct.unpack("<%dh" % (len(body) // 2), body)
        mono = bytearray()
        for i in range(0, len(s), 2):
            mono += struct.pack("<h", (s[i] + s[i + 1]) // 2)
        body = bytes(mono)
    return rate, body


def snr_db(reference, decoded, align=0):
    """SNR at the playback layer, tolerant of codec lookahead.

    Opus carries an algorithmic delay (mdct overlap + prefilter), so a
    naive sample-indexed diff measures the SHIFT, not the codec. The
    error is minimised over a small alignment window first, then SNR
    is computed on the aligned core.
    """
    n = min(len(reference), len(decoded)) // 2
    if n == 0:
        return -1.0
    a = struct.unpack("<%dh" % n, reference[:n * 2])
    b = struct.unpack("<%dh" % n, decoded[:n * 2])

    probe = 8192
    pa = [x / 16384.0 for x in a[:probe]]
    pb = [x / 16384.0 for x in b[:probe]]
    best, best_i = None, 0
    for off in range(0, align + 1):
        e = sum((pa[k] - pb[k + off]) * (pa[k] - pb[k + off])
                for k in range(probe - off))
        if best is None or e < best:
            best, best_i = e, off
    if align:
        b = b[best_i:]
        n -= best_i
    a = a[:n]
    b = b[:n]
    err = sum((x - y) * (x - y) for x, y in zip(a, b))
    sig = sum(x * x for x in a)
    if err == 0:
        return 999.0
    return 10.0 * math.log10(max(sig, 1.0) / err)
