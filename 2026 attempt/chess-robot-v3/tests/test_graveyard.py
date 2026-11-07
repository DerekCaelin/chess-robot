# test_graveyard.py - 'remember where the opponent puts pieces in the
# graveyard' (2026-11-08): capture detection (en passant included), the
# placement memory, the takeback rewind that frees slots, the human slot
# descriptions, and the game_state round-trip that makes the memory survive
# an app restart.

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chessapp import game_state                      # noqa: E402
from chessapp.graveyard import (GraveMemory, captured_on,  # noqa: E402
                                piece_name, slot_desc)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def board_from(rows):
    """rows: 8 strings, rank 8 first ('.' empty)."""
    return [list(r) for r in rows]


START = board_from([
    "rnbqkbnr", "pppppppp", "........", "........",
    "........", "........", "PPPPPPPP", "RNBQKBNR",
])


def with_pieces(*placements):
    b = board_from(["........"] * 8)
    for sq, piece in placements:
        b[8 - int(sq[1])][ord(sq[0]) - 97] = piece
    return b


# ── captured_on ─────────────────────────────────────────────────────────────
b = with_pieces(("e4", "P"), ("d5", "p"))
check(captured_on(b, "e4d5") == ("p", "d5"), "pawn capture not seen")
check(captured_on(START, "e2e4") is None, "quiet move read as a capture")

# en passant: the victim sits BESIDE the target square
b = with_pieces(("e5", "P"), ("d5", "p"))
check(captured_on(b, "e5d6") == ("p", "d5"), "en passant victim missed")

# a non-pawn diagonal onto an empty square is no capture
b = with_pieces(("e4", "N"))
check(captured_on(b, "e4d6") is None, "knight hop read as a capture")

# promotion capture
b = with_pieces(("a7", "P"), ("b8", "r"))
check(captured_on(b, "a7b8q") == ("r", "b8"), "promotion capture missed")

# ── piece names and slot descriptions ───────────────────────────────────────
check(piece_name("N") == "white knight", "piece_name white knight")
check(piece_name("p") == "black pawn", "piece_name black pawn")
check(slot_desc(0) == "inner column, by rank 8", "slot 0 desc: " + slot_desc(0))
check(slot_desc(5) == "outer column, by rank 6", "slot 5 desc: " + slot_desc(5))
check(slot_desc(None) == "the graveyard", "slot None desc")

# ── the memory ──────────────────────────────────────────────────────────────
g = GraveMemory()
g.record("p", "d5", slot=0, by="robot", ply=2)      # opponent's capture
g.record("N", "f6", slot=None, by="player", ply=3)  # the player's capture
g.record("P", "e5", slot=1, by="robot", ply=4)
check(g.used_slots() == [0, 1], "used_slots wrong: %s" % g.used_slots())
check(g.at("d5")["piece"] == "p", "at(d5) wrong")
check(g.at("f6")["by"] == "player", "at(f6) wrong")

# takeback to ply 2: the captures at plies 3 and 4 leave the graveyard and
# their slots come back to the arm
freed = g.rewind(2)
check(freed == [1], "freed slots wrong: %s" % freed)
check(len(g.entries) == 1 and g.entries[0]["square"] == "d5",
      "rewind kept the wrong entries")
check(g.used_slots() == [0], "used_slots after rewind")

# round-trip through to_list (what game_state persists)
g2 = GraveMemory(g.to_list())
check(g2.at("d5")["slot"] == 0, "round-trip lost the placement")

# ── game_state persistence (its own file, not the live game) ────────────────
old_path = game_state.PATH
tmp = os.path.join(tempfile.mkdtemp(), "game.json")
game_state.PATH = tmp
try:
    game_state.save(["e2e4", "d7d5"], "white", 5,
                    graveyard=[{"piece": "p", "square": "d5", "slot": 0,
                                "by": "robot", "ply": 2}])
    data = game_state.load()
    check(data is not None and data.get("graveyard", [{}])[0].get("slot") == 0,
          "graveyard did not survive the save/load round-trip")
finally:
    game_state.PATH = old_path

print("graveyard FAILS:" if fails else
      "PASS: graveyard memory (captures, slots, takeback, persistence)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
