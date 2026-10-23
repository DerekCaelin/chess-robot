# tools/eval_chessgemma.py - DESKTOP evaluation (2026-10-23).
#
# Feed NAKSTStudio/chess-gemma-commentary (Gemma-3 270M, q8_0 via ollama)
# OUR OWN analysis fields in the model's exact input format and judge:
#   * does it hallucinate (player names! piece claims that are not on the
#     board)?  The template+harvest approach failed exactly there
#   * is it informative (says what the move did / the engine's line)?
#   * tone (matter-of-fact, no drama)
#   * speed per move -> what the Pi 3B would cost
#
#   python3 tools/eval_chessgemma.py [/tmp/opencode/an_smoke.pkl]
import json
import pickle
import sys
import time
import urllib.request

SYSTEM = ("Generate professional chess commentary in the specified language. "
          "For Type=standard use 30-40 words. For Type=explanation, explain "
          "the best move briefly (<=50 words). Return exactly: Commentary, "
          "Predicted ELO, Verified Classification.")

TAGS = {"brilliant": "Brilliant", "great": "Best", "best": "Best",
        "good": "Good", "inaccuracy": "Inaccuracy", "mistake": "Mistake",
        "blunder": "Blunder"}


def user_msg(e, player="white"):
    actor = "human" if e["color"] == player else "bot"
    best = e.get("best") or ""
    cp = "CP %d->%d (D=%d)" % (e.get("eval_before_cp", 0),
                               e.get("eval_cp", 0),
                               e.get("eval_cp", 0) - e.get("eval_before_cp", 0))
    return """LanguageL: English
LangCode: en
Type: standard
FEN: %s
MoveSAN: %s
Side: %s
Actor: %s
Name: Player
Gender: neutral
Tag: %s
BestAlt: %s
%s""" % (e.get("fen_before", ""), e["san"],
         e["color"].capitalize(), actor,
         TAGS.get(e["quality"], "Good"), best, cp)


def chat(prompt, model="chess-commentary"):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 220},
    }).encode()
    req = urllib.request.Request("http://localhost:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    dt = time.time() - t
    n = d.get("eval_count") or 0
    return d.get("message", {}).get("content", ""), dt, n


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/an_smoke.pkl"
    an = pickle.load(open(path, "rb"))
    moves = an["moves"]
    # a spread: quiet, best, great, inaccuracy, mistake, blunder
    want = ["best", "good", "great", "inaccuracy", "mistake", "blunder"]
    picks = []
    for q in want:
        m = [e for e in moves if e["quality"] == q]
        if m:
            picks.append(m[len(m) // 2])
    # plus the quietest book-ish move
    quiet = [e for e in moves if e.get("quiet")]
    if quiet:
        picks.insert(0, quiet[0])
    seen = set()
    total_t = total_n = 0
    for e in picks:
        if e["ply"] in seen:
            continue
        seen.add(e["ply"])
        out, dt, n = chat(user_msg(e))
        total_t += dt
        total_n += n
        print("=" * 72)
        print("ply %d  %s (%s)  loss=%d  best_alt=%s  %d tokens in %.1fs"
              " (%.1f tok/s)" % (e["ply"], e["san"], e["quality"],
                                 e.get("loss_cp", 0), e.get("best_san", ""),
                                 n, dt, n / dt if dt else 0))
        print("IN :", user_msg(e).replace("\n", " | ")[:150])
        print("OUT:", out.replace("\n", " "))
    if seen:
        print("=" * 72)
        print("mean: %.1fs and %.1f tok/s per comment" %
              (total_t / len(seen), total_n / total_t if total_t else 0))


if __name__ == "__main__":
    main()
