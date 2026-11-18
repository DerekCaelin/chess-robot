# tests/test_playermove_match.py - the vision matcher's change->move logic.
#
# The camera part is mocked (piece_detect.monitor returns a synthetic list
# of changed squares); everything else is the REAL getplayermove().  The
# en-passant case is the regression this test was written for (2026-11-06):
# the capture changes THREE squares and the captured pawn's square used to
# be filed as a second destination, so 'd5xe6' built 'd5e5' and the game
# called a legal en passant illegal.
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import playermove_rpd as PM  # noqa: E402
import piece_detect  # noqa: E402

JOSTLE = piece_detect.JOSTLE_THRESH


def board_with(squares):
    """squares: {(row, col): piece} -> an 8x8 board array."""
    b = [["." for _ in range(8)] for _ in range(8)]
    for (r, c), piece in squares.items():
        b[r][c] = piece
    return b


def match(board, changes, mover_white=True):
    piece_detect.monitor = lambda **kw: list(changes)
    return PM.getplayermove(board, [], mover_white=mover_white)


def sq(name):
    "e6 -> (row, col) with row 0 = rank 8, col 0 = the a-file"
    return 8 - int(name[1]), "abcdefgh".index(name[0])


def main():
    fails = []

    def check(label, got, want):
        ok = got == want
        print("%-38s %-8s %s" % (label, got, "" if ok else "(want %s)" % want))
        if not ok:
            fails.append(label)

    # ── the bug: white d5xe6 e.p. ────────────────────────────────────────
    # three squares change: d5 (mover leaves), e6 (mover arrives),
    # e5 (the captured pawn vanishes).  e5 held a BLACK piece before the
    # move and must NOT be taken as a destination - in either scan order.
    b = board_with({sq("d5"): "P", sq("e5"): "p", sq("e1"): "K", sq("e8"): "k"})
    chg = [(sq("d5")[0], sq("d5")[1], 10.0),
           (sq("e6")[0], sq("e6")[1], 9.0),
           (sq("e5")[0], sq("e5")[1], 8.0)]
    check("white en passant d5xe6", match(b, chg), "md5e6")
    check("white en passant (reversed scan)", match(b, list(reversed(chg))),
          "md5e6")
    check("white en passant (captured first)",
          match(b, [chg[2], chg[0], chg[1]]), "md5e6")

    # ── black en passant: black pawn d4xe3 e.p., white pawn e4 removed ──
    b = board_with({sq("d4"): "p", sq("e4"): "P", sq("e1"): "K", sq("e8"): "k"})
    chg = [(sq("d4")[0], sq("d4")[1], 10.0),
           (sq("e3")[0], sq("e3")[1], 9.0),
           (sq("e4")[0], sq("e4")[1], 8.0)]
    check("black en passant d4xe3", match(b, chg, mover_white=False), "md4e3")

    # ── regressions: ordinary moves still parse ─────────────────────────
    b = board_with({sq("a2"): "P", sq("e1"): "K", sq("e8"): "k"})
    check("white quiet a2a4",
          match(b, [(sq("a2")[0], sq("a2")[1], 10.0),
                    (sq("a4")[0], sq("a4")[1], 9.0)]), "ma2a4")

    b = board_with({sq("d5"): "P", sq("e6"): "p", sq("e1"): "K", sq("e8"): "k"})
    check("white normal capture d5xe6",
          match(b, [(sq("d5")[0], sq("d5")[1], 10.0),
                    (sq("e6")[0], sq("e6")[1], 9.0)]), "md5e6")

    b = board_with({sq("e7"): "p", sq("e1"): "K", sq("e8"): "k"})
    check("black quiet e7e5",
          match(b, [(sq("e7")[0], sq("e7")[1], 10.0),
                    (sq("e5")[0], sq("e5")[1], 9.0)], mover_white=False),
          "me7e5")

    # ── castling (king + rook both leave), both colours ─────────────────
    b = board_with({sq("e1"): "K", sq("h1"): "R", sq("e8"): "k"})
    check("white castles short",
          match(b, [(sq("e1")[0], sq("e1")[1], 10.0),
                    (sq("g1")[0], sq("g1")[1], 9.0),
                    (sq("h1")[0], sq("h1")[1], 8.0)]), "me1g1")
    b = board_with({sq("e8"): "k", sq("h8"): "r", sq("e1"): "K"})
    check("black castles short",
          match(b, [(sq("e8")[0], sq("e8")[1], 10.0),
                    (sq("g8")[0], sq("g8")[1], 9.0),
                    (sq("h8")[0], sq("h8")[1], 8.0)], mover_white=False),
          "me8g8")

    # ── the accepted ep move must also be LEGAL on the game board ───────
    import ChessBoard
    cb = ChessBoard.ChessBoard()
    for mv in ["e2e4", "d7d5", "e4d5", "e7e5"]:
        assert cb.addTextMove(mv), mv
    check("ChessBoard accepts d5e6", cb.addTextMove("d5e6"), True)

    if fails:
        print("FAIL:", ", ".join(fails))
        raise SystemExit(1)
    print("PASS: player-move matching (en passant + regressions)")


if __name__ == "__main__":
    main()
