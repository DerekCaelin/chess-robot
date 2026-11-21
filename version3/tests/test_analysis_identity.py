# test_analysis_identity.py - a game's analysis identity is its MOVES
# (2026-11-21): the old hash covered the whole saved dict, so the same game
# forked into several identities whenever a writer touched the bookkeeping
# (result filled at game end, stars, graveyard, level, 'started') - the
# runs scattered across cache dirs and the analysis screen tracked a dead
# identity, showing 0/42 while the engine worked under another name.
#   python3 tests/test_analysis_identity.py

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chessapp.analysispack import game_hash, AnalysisPack   # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


base = {
    "moves": ["d2d4", "e7e6", "c2c4"],
    "player_color": "white",
    "level": 5,
    "result": None,
    "started": "2026-11-21T00:01:02",
    "stars": [3, 33],
    "graveyard": [{"piece": "p", "slot": 1}],
}
h0 = game_hash(base)

# the bookkeeping must NOT fork the identity
for field, value in (("result", "1-0"), ("result", "0-1"),
                     ("stars", []), ("stars", [1, 2, 3, 4, 5]),
                     ("graveyard", []), ("level", 2),
                     ("started", "2026-11-22T09:00:00")):
    d = dict(base)
    d[field] = value
    check(game_hash(d) == h0,
          "%s forked the identity: %s" % (field, game_hash(d)))

# ...and an absent bookkeeping key must not either (the game-over snapshot
# omits 'graveyard' entirely)
d = {k: v for k, v in base.items() if k != "graveyard"}
check(game_hash(d) == h0, "omitting graveyard forked the identity")

# the GAME does define the identity
d = dict(base, moves=base["moves"] + ["b8c6"])
check(game_hash(d) != h0, "different moves share one identity")
d = dict(base, player_color="black")
check(game_hash(d) != h0, "different player side shares one identity")

# two pack instances of the same game agree (the screen's pack and the
# background worker's pack must key the same run)
check(AnalysisPack(dict(base)).hash == AnalysisPack(
    dict(base, result="1-0", stars=[])).hash,
    "two packs of one game disagree on the identity")

print("analysis-identity FAILS:" if fails else
      "PASS: analysis identity (moves only; bookkeeping cannot fork it)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
