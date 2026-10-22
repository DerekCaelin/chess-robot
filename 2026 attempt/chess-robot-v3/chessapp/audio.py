# audio.py - central speech/volume so settings apply everywhere.
#
# Replaces ad-hoc pyttsx3 usage: game/UI code calls speak(); volume and mute
# come from settings (audio.volume / audio.muted / audio.rate). amixer master
# is set best-effort when the platform has it.

import subprocess

_engine = None
_engine_failed = False


def _tts():
    global _engine, _engine_failed
    if _engine is None and not _engine_failed:
        try:
            import pyttsx3
            from chessapp import settings
            _engine = pyttsx3.init()
            _engine.setProperty("rate", settings.get("audio.rate", 125))
            _engine.setProperty("volume", settings.get("audio.volume", 80) / 100.0)
        except Exception:
            _engine_failed = True
    return _engine


def apply_volume():
    """Push settings to ALSA master (best effort) and refresh the TTS engine."""
    from chessapp import settings
    global _engine
    vol = 0 if settings.get("audio.muted", False) else settings.get("audio.volume", 80)
    try:
        subprocess.run(["amixer", "sset", "Master", "%d%%" % vol],
                       capture_output=True, timeout=3)
    except Exception:
        pass                      # no amixer / digital-only output: fine
    _engine = None               # rebuild with new rate/volume next speak()


def speak(text):
    from chessapp import settings
    if settings.get("audio.muted", False) or not text:
        return
    eng = _tts()
    if eng is None:
        return
    try:
        eng.say(text)
        eng.runAndWait()
    except Exception:
        pass
