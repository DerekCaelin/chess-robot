# test_speech.py - the game phrases spoken in the chosen voice (2026-11-14):
# hello at the start, check, checkmate win/lose, stalemate.  A phrase whose
# sound is not ready falls back to the system TTS (never silent, never
# blocking); 'none' says nothing.

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chessapp import speech as SP          # noqa: E402
from chessapp import audio                 # noqa: E402
from chessapp import voice as VO           # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def main():
    # the phrase set covers the specified triggers
    for k in ("hello", "check", "mate_win", "mate_lose", "stalemate"):
        check(k in SP.PHRASES, "missing phrase %s" % k)
    check("you win" in SP.PHRASES["mate_win"].lower(), "mate_win wording")
    check("you lose" in SP.PHRASES["mate_lose"].lower(), "mate_lose wording")

    tmp = tempfile.mkdtemp()
    old_dir, old_voice = SP.DIR, SP.voice
    old_play, old_speak = audio.play_wav, audio.speak
    old_espeak = VO.synth_espeak
    old_many = VO.synth_piper_many
    SP.DIR = tmp
    played, spoken, made = [], [], []
    audio.play_wav = lambda p: played.append(p) or True
    audio.speak = lambda t: spoken.append(t)
    VO.synth_espeak = lambda t, o: made.append(o)
    VO.synth_piper_many = lambda items, **k: [made.append(o) for _, o in items]
    try:
        # 'none' is silent
        SP.voice = lambda: "none"
        SP.say("check")
        check(not played and not spoken, "the none voice spoke")

        # a missing sound falls back to the system TTS and warms the set
        SP.voice = lambda: "espeak"
        warmed = []
        old_warm = SP.warm
        SP.warm = lambda v=None, background=True: warmed.append(v)
        SP.say("check")
        check(spoken == ["Check."], "fallback did not speak the words: %s"
              % spoken)
        check(warmed == ["espeak"], "the fallback did not warm the voice")
        SP.warm = old_warm
        SP.warm("espeak", background=False)
        check(len(made) == len(SP.PHRASES),
              "warm made %d sounds, expected %d" % (len(made),
                                                    len(SP.PHRASES)))
        # warm_all cooks every voice, selected first
        made.clear()
        SP.voice = lambda: "glados"
        SP.warm_all(background=False)
        dirs = sorted({os.path.basename(os.path.dirname(o)) for o in made})
        check(dirs == ["alba", "espeak", "glados"],
              "warm_all missed voices: %s" % dirs)

        # with the sound present, it plays the wav (not the TTS)
        SP.voice = lambda: "espeak"
        spoken.clear()
        os.makedirs(os.path.join(tmp, "espeak"), exist_ok=True)
        with open(os.path.join(tmp, "espeak", "check.wav"), "w") as f:
            f.write("x")
        SP.say("check")
        check(played and played[-1].endswith("check.wav"),
              "the ready wav was not played")
        check(not spoken, "the TTS spoke although the wav was ready")
    finally:
        SP.DIR, SP.voice = old_dir, old_voice
        audio.play_wav, audio.speak = old_play, old_speak
        VO.synth_espeak = old_espeak
        VO.synth_piper_many = old_many
        shutil.rmtree(tmp, ignore_errors=True)


main()
print("speech FAILS:" if fails else
      "PASS: game phrases (voice, fallback, cache warm)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
