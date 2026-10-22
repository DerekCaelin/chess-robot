# chessapp/voice.py - speak the narrator's transcript. Two offline engines:
#   piper  - neural (en_GB-alba-medium), the quality voice
#   espeak - the tiny fallback the robot already has
#
# Renders a whole transcript to .wav, sentence by sentence (which is also how
# it will feed the UI later: first sentence out while the rest is still
# synthesizing).
#
#   python3 -m chessapp.voice data/narrative_last.txt --engine both
#   python3 -m chessapp.voice --engine piper            # from the cached game
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICES = os.path.join(ROOT, "data", "voices")
SAMPLES = os.path.join(ROOT, "data", "voice_samples")
PIPER_MODEL = os.path.join(VOICES, "en_GB-alba-medium.onnx")
ESPEAK_VOICE = "en-gb"


def sentences(text):
    """Speakable chunks: split at sentence ends, keep the punctuation."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


def _merge_wavs(paths, out, pause=0.35):
    """Concatenate same-format wavs with silence between them. Refuses to
    blend mismatched formats (that is where the static came from)."""
    first = wave.open(paths[0], "rb")
    params = first.getparams()
    rate = first.getframerate()
    frames = [first.readframes(first.getnframes())]
    first.close()
    fill = b"\x80" if params.sampwidth == 1 else b"\x00"   # 8-bit silence!
    silence = fill * int(rate * params.sampwidth * params.nchannels * pause)
    for p in paths[1:]:
        w = wave.open(p, "rb")
        wp = w.getparams()
        if (wp.nchannels, wp.sampwidth, wp.framerate, wp.comptype) != \
                (params.nchannels, params.sampwidth, params.framerate,
                 params.comptype):
            w.close()
            raise SystemExit("wav format mismatch: %s" % p)
        frames.append(silence)
        frames.append(w.readframes(w.getnframes()))
        w.close()
    outw = wave.open(out, "wb")
    outw.setparams(params)
    for f in frames:
        outw.writeframes(f)
    outw.close()


def synth_piper(text, out, model=PIPER_MODEL):
    """Synthesize with Piper, one sentence at a time, then join."""
    if not os.path.exists(model):
        raise SystemExit("missing voice model: %s" % model)
    parts = []
    with tempfile.TemporaryDirectory() as td:
        for i, s in enumerate(sentences(text)):
            wav = os.path.join(td, "p%03d.wav" % i)
            r = subprocess.run([sys.executable, "-m", "piper", "-m", model,
                                "-f", wav],
                               input=s, text=True, capture_output=True)
            if r.returncode != 0 or not os.path.exists(wav):
                raise SystemExit("piper failed on %r: %s" % (s, r.stderr[-400:]))
            parts.append(wav)
        _merge_wavs(parts, out)
    return out


def synth_espeak(text, out, voice=ESPEAK_VOICE, speed=155):
    """Synthesize with espeak (the small fallback engine). ONE pass for the
    whole track: espeak inserts the sentence pauses itself, and splitting it
    into per-sentence files was producing a burst of static at every seam."""
    if shutil.which("espeak") is None:
        raise SystemExit("espeak not installed")
    r = subprocess.run(["espeak", "-v", voice, "-s", str(speed),
                        "-w", out, text], capture_output=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise SystemExit("espeak failed: %s" % r.stderr[-400:])
    return out


def extract_track(path, style="speech"):
    """Pull the SPEECH (or SCREEN) section out of a saved narrative."""
    text = open(path).read()
    key = "=== %s ===" % style.upper()
    if key in text:
        text = text.split(key, 1)[1]
        for other in ("=== SCREEN ===", "=== SPEECH ==="):
            if other in text:
                text = text.split(other, 1)[0]
    return text.strip()


def speak_text(text, engine, out, model=PIPER_MODEL):
    if engine == "piper":
        return synth_piper(text, out, model=model)
    return synth_espeak(text, out)


PIPER_MODELS = {
    "alba": os.path.join(VOICES, "en_GB-alba-medium.onnx"),
    "alan": os.path.join(VOICES, "en_GB-alan-low.onnx"),
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="render a narrative to audio")
    ap.add_argument("textfile", nargs="?",
                    default=os.path.join(ROOT, "data", "narrative_last.txt"))
    ap.add_argument("--engine", choices=("piper", "espeak", "both"),
                    default="both")
    ap.add_argument("--model", choices=tuple(PIPER_MODELS), default="alba",
                    help="which Piper voice (alba=medium, alan=low)")
    ap.add_argument("--style", choices=("speech", "screen"), default="speech")
    ap.add_argument("--out", default=SAMPLES)
    ap.add_argument("--name", default=None)
    a = ap.parse_args(argv)

    text = extract_track(a.textfile, a.style)
    if not text:
        raise SystemExit("no %s track found in %s" % (a.style, a.textfile))
    os.makedirs(a.out, exist_ok=True)
    base = a.name or (os.path.splitext(os.path.basename(a.textfile))[0]
                      + "_" + a.style)
    engines = ("piper", "espeak") if a.engine == "both" else (a.engine,)
    made = []
    for eng in engines:
        tag = eng if eng != "piper" else "piper_" + a.model
        out = os.path.join(a.out, "%s_%s.wav" % (base, tag))
        print("synthesizing %s -> %s" % (tag, out))
        try:
            speak_text(text, eng, out, model=PIPER_MODELS[a.model])
        except SystemExit as e:
            print("  SKIPPED:", e)
            continue
        w = wave.open(out, "rb")
        print("  %.1f s of audio" % (w.getnframes() / w.getframerate()))
        w.close()
        made.append(out)
    return made


if __name__ == "__main__":
    main()
