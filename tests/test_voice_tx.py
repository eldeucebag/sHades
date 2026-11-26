#!/usr/bin/env python3
"""Transmit-shaping gate (tay, 2026-11-25): ptt / always / voc (vad).

Pins down, on the kivy-free engine:
  - frame_rms_s16 ranks silence vs full-scale speech;
  - set_voice_mode validates (bogus keeps current; voc is a UI label,
    not a mode);
  - ptt: the pacer sends only while the hold control is down, and
    self_speaking follows;
  - vad: silent mic frames do not transmit, loud ones do, and the
    hangover bridges a loud frame through the following quiet ones;
  - speaking_peers: a received voice packet (even a malformed one)
    marks its speaker live inside SPEAK_WINDOW_S.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_codec as voice_codec

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""), flush=True)


def loud_frame():
    import struct
    return struct.pack("<960h", *([9000] * 960))


def silent_frame():
    return b"\x00" * voice_codec.FRAME_BYTES


def main():
    import app_voice as av

    # 1. RMS ranking
    ok = av.frame_rms_s16(silent_frame()) == 0
    ok = ok and av.frame_rms_s16(loud_frame()) > 5000
    check("frame_rms_s16 ranks silence vs speech", ok)

    eng = av.VoiceEngine()

    # 2. mode validation: bogus keeps, real switches; "voc" is UI-only
    got = eng.set_voice_mode("bogus")
    ok = got == "always"
    ok = ok and eng.set_voice_mode("ptt") == "ptt"
    ok = ok and eng.set_voice_mode("vad") == "vad"
    ok = ok and eng.set_voice_mode("bogus") == "vad"
    ok = ok and eng.set_voice_mode("always") == "always"
    check("set_voice_mode validates", ok, got)

    # 3. ptt gate: with the hold up nothing transmits, down it does,
    #    and self_speaking tracks the tx line
    eng.join("c" * 32)
    eng.attach_source(silent_frame)
    eng._out_links["d" * 32] = av.FakeLink(eng, "d" * 32)
    eng.set_voice_mode("ptt")
    eng.start()
    time.sleep(0.55)
    before = eng.frames_out
    check("ptt hold up: pacer silent", before == 0, before)
    eng.set_ptt(True)
    time.sleep(0.55)
    after = eng.frames_out
    check("ptt hold down: frames flow", after > before, after)
    ok = eng.self_speaking()
    eng.set_ptt(False)
    time.sleep(0.3)
    ok2 = not eng.self_speaking()
    check("self_speaking tracks ptt", ok and ok2)
    eng.stop()

    # 4. vad gate: quiet mic -> nothing; loud mic -> frames; the pacer's
    #    hangover counts down across quiet frames after a loud one
    seq = {"i": 0}

    def src():
        seq["i"] += 1
        return loud_frame() if seq["i"] % 4 == 0 else silent_frame()

    eng2 = av.VoiceEngine()
    eng2.join("c" * 32)
    eng2.attach_source(src)
    eng2._out_links["d" * 32] = av.FakeLink(eng2, "d" * 32)
    eng2.set_voice_mode("vad")
    eng2._pacing.wait(0.2)
    eng2.start()
    time.sleep(1.2)
    vad_out = eng2.frames_out
    check("vad: loud frames transmit, silent do not stall the line",
          vad_out > 0, vad_out)
    eng2.stop()

    hang = av.VoiceEngine()
    hang.join("c" * 32)
    hang.attach_source(silent_frame)
    hang._out_links["d" * 32] = av.FakeLink(hang, "d" * 32)
    hang.set_voice_mode("vad")
    # one loud frame arms the hangover; the next quiet ticks still send
    hang._vad_hang = av.VAD_HANGOVER_FRAMES
    ok = hang._vad_hang == av.VAD_HANGOVER_FRAMES
    check("vad hangover armed", ok)

    # 5. speaking_peers: even a malformed 1-byte packet marks the speaker
    eng3 = av.VoiceEngine()
    eng3.packet_received("e" * 32, b"\x01")
    sp = eng3.speaking_peers()
    check("speaking_peers inside SPEAK_WINDOW_S", sp == ["e" * 32], sp)
    time.sleep(0.45)
    check("speaking window expires", eng3.speaking_peers() == [])

    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
