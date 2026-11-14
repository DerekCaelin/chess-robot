# speech.py - the short GAME phrases spoken in the player's chosen voice
# (2026-11-14): 'Hello. Let's play chess.' at the start, 'Check.' on check,
# the checkmate and stalemate lines.  Each phrase is synthesised ONCE per
# voice and cached under data/voices/phrases/<voice>/; the set is warmed in
# the background when the voice changes so a mid-game trigger never waits on
# a model load.  A phrase whose sound is not ready yet falls back to the
# system TTS with the same words - never silent, never blocking.

import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "data", "voices", "phrases")

PHRASES = {
    "hello": "Hello. Let's play chess.",
    "check": "Check.",
    "mate_win": "Checkmate. You win.",
    "mate_lose": "Checkmate. You lose.",
    "stalemate": "Stalemate. The game is a draw.",
}


def voice():
    """The selected voice ('none' | 'espeak' | 'alba' | 'glados')."""
    from chessapp import settings
    from chessapp import analysispack as AP
    return AP.norm_voice(settings.get("analysis.voice", "none"))


def path(key, v=None):
    return os.path.join(DIR, v or voice(), key + ".wav")


def warm(v=None, background=True):
    """Synthesise every missing phrase for the voice (default: the
    selected one).  ONE model load covers the whole set - piper needs
    ~15 s to load and the phrases themselves are seconds each."""
    v = v or voice()
    if v == "none":
        return

    def run():
        try:
            from chessapp import voice as VO
            os.makedirs(os.path.join(DIR, v), exist_ok=True)
            todo = [(PHRASES[k], path(k, v)) for k in sorted(PHRASES)
                    if not os.path.exists(path(k, v))]
            if not todo:
                return
            if v == "espeak":
                for text, out in todo:
                    VO.synth_espeak(text, out)
            else:
                VO.synth_piper_many(todo, model=VO.PIPER_MODELS[v])
        except Exception:
            pass

    if background:
        # NOT a daemon: a daemon mid-synthesis aborts in native code at
        # interpreter shutdown (the test suite caught it, 2026-11-14).
        # The set is cached, so this join happens once per voice.
        threading.Thread(target=run).start()
    else:
        run()


def warm_all(background=True):
    """Pre-cook EVERY voice's phrase set (2026-11-14): switching voices
    then never costs a wait.  Each set is one model load plus five short
    lines and is cached forever - about two minutes of background work,
    one time.  The SELECTED voice goes first so it is ready soonest."""
    order = ([voice()] + [v for v in ("espeak", "alba", "glados")
                          if v != voice()])
    order = [v for v in order if v != "none"]

    def run():
        for v in order:
            warm(v, background=False)

    if background:
        threading.Thread(target=run).start()   # joined at exit, not a daemon
    else:
        run()


def say(key):
    """Speak a phrase in the chosen voice NOW.  'none' says nothing; a
    phrase whose sound is still generating falls back to the system TTS
    with the same words (and the real voice fills in for next time)."""
    from chessapp import settings
    from chessapp import audio
    text = PHRASES.get(key)
    if not text or voice() == "none":
        return
    if settings.get("audio.muted", False):
        return
    wav = path(key)
    if os.path.exists(wav):
        audio.play_wav(wav)
        return
    warm(voice())
    audio.speak(text)          # the same words, without the wait
