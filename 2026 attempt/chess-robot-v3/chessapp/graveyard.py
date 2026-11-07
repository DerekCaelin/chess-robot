# graveyard.py - where captured pieces go in the graveyard, and WHERE each
# one was put (2026-11-08).
#
# The arm drops its captures into a 14-slot grid beside the a-file (two
# columns x seven rows, rows aligned to ranks 8..2 - see
# LEGO_Chess_rpd.grave_slot).  The player's own captures are placed by hand.
# The memory records which piece sits where, so:
#   * a takeback can point the player at the exact spot to fetch the piece
#     that comes back to the board ('remember where the opponent puts pieces
#     in the graveyard'), and
#   * the fill state survives an app restart - the arm must never drop a
#     capture onto a slot that is already occupied.

PIECE_NAMES = {"p": "pawn", "n": "knight", "b": "bishop", "r": "rook",
               "q": "queen", "k": "king"}


def piece_name(piece):
    """'N' -> 'white knight', 'p' -> 'black pawn'."""
    return "%s %s" % ("white" if piece.isupper() else "black",
                      PIECE_NAMES.get(piece.lower(), "piece"))


def slot_desc(slot):
    """Human phrase for a graveyard slot.  Even slots are the INNER column
    (next to the board); rows walk rank 8 -> 2."""
    if slot is None:
        return "the graveyard"
    col = "inner" if slot % 2 == 0 else "outer"
    return "%s column, by rank %d" % (col, 8 - slot // 2)


def square_of(board, sq):
    """Layout char at a square name ('e4'); getBoard() rows: 0 = rank 8."""
    return board[8 - int(sq[1])][ord(sq[0]) - 97]


def captured_on(board, move):
    """The (piece, square) a UCI move captures on the given board layout -
    None when the move is not a capture.  En passant included: the victim
    sits beside the target, not on it."""
    frm, to = move[:2], move[2:4]
    victim = square_of(board, to)
    if victim != ".":
        return victim, to
    mover = square_of(board, frm)
    # a pawn diagonally onto an empty square = en passant; the victim sits
    # on the mover's own rank, under the target file
    if mover.lower() == "p" and frm[0] != to[0]:
        sq = to[0] + frm[1]
        v = square_of(board, sq)
        if v != ".":
            return v, sq
    return None


class GraveMemory:
    """The remembered placements: the piece, the square it was captured from,
    the graveyard slot it landed in (None when a hand or the classic spot
    placed it), who put it there, and the ply it happened on."""

    def __init__(self, entries=None):
        self.entries = [dict(e) for e in (entries or [])]

    def record(self, piece, square, slot=None, by="robot", ply=0):
        self.entries.append({"piece": piece, "square": square, "slot": slot,
                             "by": by, "ply": int(ply)})

    def rewind(self, ply):
        """Take back to `ply` moves: every later capture leaves the graveyard
        (its piece goes back on the board).  Returns the freed slot indexes,
        so the arm can reuse them instead of skipping past empty slots."""
        freed = sorted({e["slot"] for e in self.entries
                        if e["ply"] > ply and e.get("slot") is not None})
        self.entries = [e for e in self.entries if e["ply"] <= ply]
        return freed

    def used_slots(self):
        return sorted({e["slot"] for e in self.entries
                       if e.get("slot") is not None})

    def at(self, square):
        """The newest piece captured FROM this square - the piece a takeback
        brings back to `square` is the one in the graveyard."""
        for e in reversed(self.entries):
            if e["square"] == square:
                return e
        return None

    def to_list(self):
        return [dict(e) for e in self.entries]
