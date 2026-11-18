# test_promotion.py - a promotion must carry its piece (2026-11-14): the
# vision reports squares only, and the 4-char monitor message dropped the
# promotion piece - the move landed as a pawn on the 8th, check went
# unannounced and the engine never moved.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chessapp.game_controller import resolve_promotion   # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def board_with(pieces):
    b = [["."] * 8 for _ in range(8)]
    for sq, p in pieces:
        b[8 - int(sq[1])][ord(sq[0]) - 97] = p
    return b


# a white pawn reaching the 8th: the queen is assumed without a prompt
w = board_with([("e7", "P"), ("e8", ".")])
check(resolve_promotion("e7e8", w) == "e7e8q",
      "white promotion default: %r" % resolve_promotion("e7e8", w))
# the prompt's answer wins
check(resolve_promotion("e7e8", w, ask=lambda mv: "n") == "e7e8n",
      "the prompt answer was ignored")
check(resolve_promotion("e7e8", w, ask=lambda mv: "zzz") == "e7e8q",
      "a bad prompt answer was not rejected")
# black promotes the other way
b = board_with([("d2", "p")])
check(resolve_promotion("d2d1", b) == "d2d1q",
      "black promotion default: %r" % resolve_promotion("d2d1", b))
# nothing else changes: quiet moves, captures, already-promoted moves
check(resolve_promotion("e7e6", w) == "e7e6", "a quiet move was rewritten")
check(resolve_promotion("e7d8", w) == "e7d8q",
      "a pawn capture onto the 8th was not treated as a promotion")
check(resolve_promotion("e7e8q", w) == "e7e8q", "a full UCI was rewritten")
nonpawn = board_with([("e7", "Q")])
check(resolve_promotion("e7e8", nonpawn) == "e7e8",
      "a queen move was rewritten")

print("promotion FAILS:" if fails else
      "PASS: promotions carry their piece (default queen, prompt wins)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
