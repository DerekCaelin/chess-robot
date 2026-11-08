# test_prewarm.py - the game analysis grows in the background while the
# game is played (2026-11-08): entries are per POSITION, so the work done
# on the first N moves carries into the longer game - the absorb copies it
# forward and only the NEW moves get searched.  Also: stale caches are
# cleaned up, and prewarm prefers the game being played.

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chessapp import analysispack as AP          # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class FakeState:
    def __init__(self):
        self.live = None
        self.last = None

    def load(self):
        return self.live

    def load_last(self):
        return self.last


def fake_analyze(seen):
    """analyze_game stand-in honouring the cached= contract."""
    def run(g, depth=18, multipv=3, engine=None, progress=None, stop=None,
            on_entry=None, cached=None, checkpoint=None):
        ucis = [m.uci() for m in g.mainline_moves()]
        cached = list(cached or [])
        entries = []
        for i, u in enumerate(ucis):
            if i < len(cached) and cached[i].get("uci") == u:
                entries.append(cached[i])
            else:
                seen.append(u)
                entries.append({"ply": i + 1, "uci": u, "san": "x",
                                "eval_cp": 0, "eval_mate": None})
            if on_entry is not None:
                on_entry(entries[-1])
        if checkpoint is not None:
            checkpoint(entries, ucis, [])
        return {"meta": {}, "moves": entries, "openings": [],
                "turning_points": [], "decision_points": [],
                "quiet_runs": [], "phases": [], "eval_words": {}}
    return run


def game(n):
    mv = (["g1f3", "g8f6", "f3g1", "f6g8"] * ((n + 3) // 4))[:n]
    return {"moves": mv, "player_color": "white", "level": 5,
            "started": "2026-01-01T00:00:00", "result": "*"}


tmp = tempfile.mkdtemp()
old_cache, old_state = AP.CACHE, AP.game_state
old_an, old_pm = AP.AN, AP.NR
AP.CACHE = os.path.join(tmp, "analysis")
os.makedirs(AP.CACHE, exist_ok=True)
state = FakeState()
AP.game_state = state


class _NR:
    @staticmethod
    def per_move(analysis):
        return analysis["moves"]


try:
    seen = []
    AP.AN = type("AN", (), {"analyze_game": staticmethod(fake_analyze(seen)),
                            "analysis_to_jsonable":
                                staticmethod(lambda a: a)})
    AP.NR = _NR

    # game 1: 4 moves are analysed from scratch
    p4 = AP.AnalysisPack(game(4))
    p4.generate(voice="none", text_only=True)
    check(seen == game(4)["moves"], "run1 analysed %s" % seen)
    check(os.path.isdir(p4.dir), "run1 cache dir missing")

    # the game grows to 6 moves: the first 4 come from the cache
    seen.clear()
    p6 = AP.AnalysisPack(game(6))
    p6.generate(voice="none", text_only=True)
    check(seen == game(6)["moves"][4:], "run2 should analyse ONLY the new "
          "moves, analysed %s" % seen)
    check(not os.path.isdir(p4.dir), "absorbed cache was not cleaned up")
    check(len(p6.moves) == 6, "run2 text covers %d moves" % len(p6.moves))

    # an earlier game's cache is discarded, not absorbed
    other = os.path.join(AP.CACHE, "zzz_other")
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "text.partial.json"), "w") as f:
        json.dump({"format": AP.CACHE_FORMAT, "code": AP._code_digest(),
                   "depth": 12, "ucis": ["e2e4"], "entries": [{"uci": "e2e4"}],
                   "openings": []}, f)
    seen.clear()
    p8 = AP.AnalysisPack(game(8))
    p8.generate(voice="none", text_only=True)
    check(not os.path.isdir(other), "an earlier game's cache survived")
    check(seen == game(8)["moves"][6:], "run3 analysed %s" % seen)

    # prewarm prefers the game being played, text only
    got = {}
    old_ga = AP.generate_async
    AP.generate_async = lambda pack, voice, depth=None, text_only=False: \
        got.update(pack=pack, text_only=text_only) or {}
    state.live = game(3)
    state.last = None
    AP.prewarm()
    check(got.get("text_only") is True and
          got["pack"].game.get("moves") == game(3)["moves"],
          "prewarm did not take the live game (text only)")
    # and falls back to the finished game when nothing is being played
    got.clear()
    state.live = None
    state.last = game(3)
    AP.prewarm()
    check(got.get("pack") is not None and got.get("text_only") is False,
          "prewarm did not fall back to the finished game")
    AP.generate_async = old_ga
finally:
    AP.CACHE, AP.game_state = old_cache, old_state
    AP.AN, AP.NR = old_an, old_pm
    shutil.rmtree(tmp, ignore_errors=True)

print("prewarm FAILS:" if fails else
      "PASS: incremental prewarm (entries carry over, only new moves searched)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
