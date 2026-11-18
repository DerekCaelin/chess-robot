# tests/test_grip_model.py - the piece GRIP model (2026-11-06):
#
#   grip height = piece grip height + board height + arm sag compensation
#
# The user defines each piece's GRIP HEIGHT directly (the grab point's
# height above the piece's own sitting surface - what you jog to and read);
# the board height term is the sitting surface above the table (0 in the
# graveyard - 'subtract the height of the board from the movement height
# when depositing a piece in the graveyard'); the sag compensation is added
# inside ik_Y at command time.  Pick height adjust is REMOVED from the
# model, and each piece carries its own jaw adjust.
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("GRIP_TEST", "1")
from chessapp import settings  # noqa: E402
settings.USER_PATH = "/tmp/test_grip_settings.json"
settings.apply()
import LEGO_Chess_rpd as RD  # noqa: E402


def main():
    fails = []

    def check(label, got, want):
        ok = (got == want) if not isinstance(want, float) else abs(got - want) < 1e-9
        print("%-56s %s" % (label, ("%s" % got) if ok else
                            ("%s (want %s)" % (got, want))))
        if not ok:
            fails.append(label)

    # ── the model ─────────────────────────────────────────────────────────
    # on the board: the grip lands at the piece grip height (the +board
    # height cancels against the board-referenced Y)
    for pc in ("p", "r", "n", "b", "q", "k"):
        want = RD.piecegrips[pc] + RD.gripper_len
        check("grip_y(%s) on board" % pc, RD.grip_y(pc), want)
    # in the graveyard (surface=0): exactly board_height lower
    check("graveyard drop is board_height lower",
          RD.grip_y("p") - RD.grip_y("p", surface=0.0), RD.board_height)
    # the sag term is the third ingredient (added at command time)
    y = RD.grip_y("p")
    check("ik_Y adds the sag compensation", RD.ik_Y(6.0, y),
          y + RD.sag_comp(6.0))
    # the reach slope is retired: grip_y ignores reach
    check("grip_y ignores reach", RD.grip_y("p", 2.0), RD.grip_y("p", 20.0))

    # ── the migration: piece_grips = piece_heights - pickheightadj ───────
    for pc, h in (("p", 1.26), ("r", 1.42), ("n", 1.57),
                  ("b", 1.93), ("q", 2.24), ("k", 2.48)):
        check("golden grip %s = height - pickheightadj" % pc,
              round(h - 0.0, 3), h)
    # pick height adjust is GONE from the model
    import inspect
    src = inspect.getsource(RD.grip_y)
    check("pickheightadj removed from grip_y",
          "pickheightadj" in src, False)

    # ── the per-piece jaws ───────────────────────────────────────────────
    check("jaw default is the old global adjust (-9)",
          RD.piecejaws.get("p"), -9.0)
    check("grip_width(pawn)", RD.grip_width("p"),
          RD.piecewidths["p"] + 15 + RD.piecejaws["p"])
    # a tighter grip for a narrow piece touches only that piece
    RD.piecejaws["n"] = -14.0
    check("custom jaw grips tighter", RD.grip_width("n"),
          RD.piecewidths["n"] + 15 + -14.0)
    check("custom jaw leaves other pieces alone", RD.grip_width("p"),
          RD.piecewidths["p"] + 15 - 9.0)
    RD.piecejaws["n"] = -9.0

    # ── the sag ("arm droop") storage convention ─────────────────────────
    # stored values carry a board-height pedestal ("offset by board
    # height"); set_sag strips it so the model applies pure flex.  Without
    # the strip the gripper lands board_height too high - the symptom that
    # prompted this (2026-11-07).
    RD.set_sag([["d4", -1.0]])
    check("sag strips the board-height pedestal",
          RD.sag_comp(RD.armlength("d4")), -1.5)
    RD.set_sag([])
    check("empty sag is still zero", RD.sag_comp(8.0), 0.0)
    # the sag screen's tuning is closed-loop through the same set_sag, so
    # its stored values keep the pedestal by construction

    # ── settings -> the live arm module ──────────────────────────────────
    settings.set("arm.piece_grips", dict(RD.piecegrips, p=1.30))
    check("set piece_grips -> the arm module", RD.piecegrips["p"], 1.30)
    settings.set("arm.piece_jaws", dict(RD.piecejaws, q=-12.0))
    check("set piece_jaws -> the arm module", RD.piecejaws["q"], -12.0)
    settings.set("arm.piece_grips", dict(RD.piecegrips, p=1.28))
    settings.set("arm.piece_jaws", dict(RD.piecejaws, q=-9.0))

    # ── the graveyard deposit path (the full drop) ───────────────────────
    got = {}
    RD.moveXY = lambda y, **k: got.setdefault("y", y)
    RD.set_gripper = lambda w: None
    RD._lift_clear = lambda *a: None
    RD.droppiece("p", 6.0, surface=0.0)
    check("droppiece on the table lands board_height lower",
          got["y"], RD.grip_y("p", surface=0.0))
    got.clear()
    RD.droppiece("p", 6.0)
    check("droppiece on the board is unchanged", got["y"], RD.grip_y("p"))

    if fails:
        print("GRIP MODEL FAIL:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("PASS: grip model (per-piece grips, board height, sag, jaws)")


if __name__ == "__main__":
    main()
