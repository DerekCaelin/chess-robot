# tools/harvest_phrases.py - ONE-TIME PC-side pipeline (2026-10-22).
#
# Mine annotated games (Icannos/chess_studies on HuggingFace: top Lichess
# studies + curated classic games, natural-language comments) for human
# commentary sentences and emit chessapp/phraselib.json - human phrasing
# keyed by (concept, polarity, colour) so the narrator can prefer it over
# its own banks. Zero runtime cost on the Pi: the JSON ships with the app.
#
# Faithfulness rules (the reason for the heavy filtering):
#   * a sentence may only say what OUR OWN metrics say about the position:
#     it is filed under (concept, sign, colour) only when its own wording's
#     polarity and subject agree with the computed concept delta
#   * context-bound sentences (squares, files/ranks, notation, arrows,
#     'the c column') are DROPPED - they cannot be reused safely
#   * questions, coaching imperatives, first/second person, exclamation and
#     drama words are dropped (the tone is matter-of-fact and constructive)
#
# Usage:  python3 tools/harvest_phrases.py   (network; caches in /tmp)
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chess  # noqa: E402
import chess.pgn  # noqa: E402
import io  # noqa: E402
from chessapp import chesspos as CP  # noqa: E402
from chessapp.analysis import _concept_deltas  # noqa: E402

DATASET = "Icannos/chess_studies"
CONFIGS = ("lichess", "others")
CACHE = "/tmp/opencode/hf_rows"
OUT = os.path.join(ROOT, "chessapp", "phraselib.json")

# concept keyword map: (regex, concept). Order matters - first match wins.
CONCEPT_RE = [
    (r"\bbishop pair\b|\btwo bishops\b", "bishop_pair"),
    (r"\bopen file|semi-?open|rooks?\b.{0,24}\bfiles?\b|seventh rank\b",
     "rook_files"),
    (r"\boutpost\b|protected square", "outposts"),
    (r"\bpawn structure\b|\bdoubled\b|\bisolated\b|\bpassed pawn|\bpawn "
     r"(chain|majority|break|island|storm)|backward pawn|weak pawn|pawn "
     r"weakness", "structure"),
    (r"\bking (safety|shelter|cover|side)|\bking is (exposed|safe|tucked)|"
     r"\bpawn shield\b|attack(ing|s)?\b.{0,20}\bking\b|fianchetto|castl",
     "king_safety"),
    (r"\bcenter\b|\bcentre\b|central (square|control|pawn|outpost)|"
     r"\bgrip on the (center|centre)", "center"),
    (r"\bdevelop|undeveloped|pieces? (out|into play|into the game)|"
     r"brings? (another|the) piece", "development"),
    (r"\bspace\b|\broom (to|for)\b|cramped|squeezed", "space"),
]
NEG_RE = re.compile(
    r"\b(weak|weaken|loses|lose|lost|slips|loosen|exposed|squeezed|cramped|"
    r"falls behind|damaged|backward|isolated|doubled|hanging|gives up|"
    r"giving up|worse|problem|trouble|liability|undermine)\b", re.I)
DRAMA_RE = re.compile(
    r"\b(brilliant|stunning|crushing|devastating|beautif|amaz|terrible|"
    r"awful|horrible|disastr|magnificen|superb|genius|spectac|blunder\b.{0,12}"
    r"!\b|unbelievab|fantastic)\b", re.I)
PRONOUN_RE = re.compile(r"\b(I|my|me|we|our|us|you|your|yours|let's)\b")
SAN_RE = re.compile(r"\b(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?"
                    r"[+#]?)\b")
SQUARE_RE = re.compile(r"\b[a-h][1-8]\b")
CONTEXT_RE = re.compile(
    r"\b[a-h]\s*(-|\s)*(file|column|rank)\b|\b(rank|file|column)\s*[1-8a-h]\b|"
    r"\barrow|\bcircle\b|diagram|\bhere (white|black)\b|move \d+", re.I)
ANNOT_RE = re.compile(r"\[%(cal|csl|eval|clk|c64)[^\]]*\]|\{|\}|\.\.\.|…|…")

MAX_PHRASES_PER_BUCKET = 60


def fetch_rows(config, start, length=100):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%d.json" % (config, start))
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("rows", [])
    url = ("https://datasets-server.huggingface.co/rows?dataset=%s&config=%s"
           "&split=train&offset=%d&length=%d"
           % (urllib.parse.quote(DATASET, safe=""), config, start, length))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.load(r)
            with open(path, "w") as f:
                json.dump(data, f)
            return data.get("rows", [])
        except Exception as e:
            if attempt == 3:
                print("  fetch failed %s@%d: %s" % (config, start, e))
                return []
            time.sleep(2 + 2 * attempt)


def clean_sentence(text):
    """The reusable core of one comment sentence, or None."""
    s = ANNOT_RE.sub(" ", text)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s.endswith("?"):
        return None
    if len(s.split()) < 4 or len(s.split()) > 14:
        return None
    if ("!" in s or '"' in s or ";" in s or ":" in s
            or PRONOUN_RE.search(s) or DRAMA_RE.search(s)
            or SAN_RE.search(s) or SQUARE_RE.search(s)
            or CONTEXT_RE.search(s) or "@" in s or "/" in s
            or any(ord(ch) > 0x2122 for ch in s)):
        return None
    if not s.endswith("."):
        s += "."
    # sentences are commentary ABOUT the position: no leading move notation
    s = s[0].upper() + s[1:]
    return s


def concept_of(sentence):
    for rx, key in CONCEPT_RE:
        if re.search(rx, sentence, re.I):
            return key
    return None


def named_colors(sentence):
    w = bool(re.search(r"\bwhite\b", sentence, re.I))
    b = bool(re.search(r"\bblack\b", sentence, re.I))
    return w, b


def polarity_of(sentence):
    return "-" if NEG_RE.search(sentence) else "+"


def main():
    rng = random.Random(20261022)
    lib = {}          # (concept, sign, colour) -> [phrases]
    stats = {"rows": 0, "comments": 0, "kept": 0, "dropped": 0}

    def add(key, phrase):
        bucket = lib.setdefault(key, [])
        low = phrase.lower()
        if any(p.lower() == low for p in bucket):
            return
        if len(bucket) < MAX_PHRASES_PER_BUCKET:
            bucket.append(phrase)
            stats["kept"] += 1

    for config in CONFIGS:
        total = 2939 if config == "lichess" else 3171
        for start in range(0, total, 100):
            rows = fetch_rows(config, start)
            for r in rows:
                pgn_text = r.get("row", {}).get("text") or ""
                if "{" not in pgn_text:
                    continue
                stats["rows"] += 1
                game = chess.pgn.read_game(io.StringIO(pgn_text))
                if game is None:
                    continue
                board = game.board()
                for node in game.mainline():
                    mv = node.move
                    comment = (node.comment or "").strip()
                    turn = board.turn
                    rep_before = CP.report(board)
                    board.push(mv)
                    r_after = CP.report(board)
                    deltas = _concept_deltas(rep_before, r_after, turn)
                    if not comment:
                        continue
                    stats["comments"] += 1
                    for raw in re.split(r"(?<=[.!?])\s+", comment):
                        sent = clean_sentence(raw)
                        if not sent:
                            stats["dropped"] += 1
                            continue
                        key = concept_of(sent)
                        if key is None:
                            stats["dropped"] += 1
                            continue
                        # the phrase's own polarity must agree with the
                        # computed delta for the colour it names
                        pol = polarity_of(sent)
                        w, b = named_colors(sent)
                        if w and b:
                            stats["dropped"] += 1     # ambiguous subject
                            continue
                        mover = "white" if turn == chess.WHITE else "black"
                        named = ("white" if w else "black" if b else "any")
                        d = deltas.get(key, 0)
                        if named == "any":
                            sign = pol           # colour-free: trust wording
                        else:
                            sign = "+" if (d > 0) == (named == mover) else "-"
                            if sign != pol:
                                # the wording disagrees with the numbers
                                stats["dropped"] += 1
                                continue
                        if abs(d) < 1 and named != "any":
                            stats["dropped"] += 1
                            continue
                        add((key, sign, named), sent)
            print("  %s %d-%d: rows=%d comments=%d kept=%d dropped=%d"
                  % (config, start, start + len(rows), stats["rows"],
                     stats["comments"], stats["kept"], stats["dropped"]))

    out = {}
    for (concept, sign, colour), phrases in sorted(lib.items()):
        rng.shuffle(phrases)
        out.setdefault(concept, {}).setdefault(sign, {})[colour] = phrases
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print("\nwrote %s" % OUT)
    print("buckets: %d  phrases: %d" % (len(lib), stats["kept"]))
    for k in sorted(lib):
        print("  %-14s %s%s: %d" % (k[0], k[1], " (" + k[2] + ")",
                                    len(lib[k])))


if __name__ == "__main__":
    main()
