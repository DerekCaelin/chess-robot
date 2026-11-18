# chessapp/openings.py - named openings and situations the narrator can cite.
# An opening fires its callout when its move sequence completes (the longest
# match wins, so 1.d4 d5 2.c4 dxc4 announces the Queen's Gambit ACCEPTED, not
# the plain Gambit). Pure data + a matcher; no engine involved.
import chess

# (name, uci sequence)
OPENINGS = [
    ("the Queen's Gambit", ["d2d4", "d7d5", "c2c4"]),
    ("the Queen's Gambit Accepted", ["d2d4", "d7d5", "c2c4", "d5c4"]),
    ("the Queen's Gambit Declined", ["d2d4", "d7d5", "c2c4", "e7e6"]),
    ("the Slav Defence", ["d2d4", "d7d5", "c2c4", "c7c6"]),
    ("the Tarrasch Defence", ["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "c7c5"]),
    ("the Sicilian Defence", ["e2e4", "c7c5"]),
    ("the French Defence", ["e2e4", "e7e6"]),
    ("the Caro-Kann Defence", ["e2e4", "c7c6"]),
    ("the Scandinavian Defence", ["e2e4", "d7d5"]),
    ("the Alekhine Defence", ["e2e4", "g8f6"]),
    ("the Pirc Defence", ["e2e4", "d7d6"]),
    ("the Modern Defence", ["e2e4", "g7g6"]),
    ("the King's Pawn Game", ["e2e4", "e7e5"]),
    ("the King's Gambit", ["e2e4", "e7e5", "f2f4"]),
    ("the Italian Game", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]),
    ("the Ruy Lopez", ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]),
    ("the Scotch Game", ["e2e4", "e7e5", "g1f3", "b8c6", "d2d4"]),
    ("the Petrov Defence", ["e2e4", "e7e5", "g1f3", "g8f6"]),
    ("the Philidor Defence", ["e2e4", "e7e5", "g1f3", "d7d6"]),
    ("the Vienna Game", ["e2e4", "e7e5", "b1c3"]),
    ("the Four Knights Game", ["e2e4", "e7e5", "g1f3", "b8c6", "b1c3",
                               "g8f6"]),
    ("the Evans Gambit", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5",
                          "b2b4"]),
    ("the Danish Gambit", ["e2e4", "e7e5", "d2d4", "e5d4", "c2c3"]),
    ("the Center Game", ["e2e4", "e7e5", "d2d4", "e5d4"]),
    ("the King's Indian Defence", ["d2d4", "g8f6", "c2c4", "g7g6"]),
    ("the Nimzo-Indian Defence", ["d2d4", "g8f6", "c2c4", "e7e6", "b1c3",
                                  "f8b4"]),
    ("the Queen's Indian Defence", ["d2d4", "g8f6", "c2c4", "e7e6", "g1f3",
                                    "b7b6"]),
    ("the Grünfeld Defence", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3",
                              "d7d5"]),
    ("the London System", ["d2d4", "d7d5", "c1f4"]),
    ("the English Opening", ["c2c4"]),
    ("the Réti Opening", ["g1f3"]),
    ("the Bird's Opening", ["f2f4"]),
]

# Named situations (used by the narrator when a fact matches a motif).
SITUATIONS = {
    "fork": ["a fork", "a double attack"],
    "pin": ["a pin"],
    "mate": ["checkmate"],
    "double_check": ["a double check"],
    "promotion": ["a promotion"],
    "passed": ["a passed pawn"],
    "isolated": ["an isolated pawn"],
    "opens_line": ["an open line"],
    "castles": ["castling"],
    "capture": ["a capture"],
    "exposes": ["a loose piece"],
    "threatens": ["a threat"],
}


def match_opening(ucis):
    """Longest opening whose sequence completes `ucis` exactly at its end.
    Returns (name, ply_of_the_final_move) or None."""
    best = None
    for name, seq in OPENINGS:
        if len(seq) <= len(ucis) and ucis[:len(seq)] == seq:
            if best is None or len(seq) > len(best[1]):
                best = (name, seq)
    if best is None:
        return None
    return best[0], len(best[1])


def opening_name_from(ucis):
    m = match_opening(ucis)
    return m[0] if m else None
