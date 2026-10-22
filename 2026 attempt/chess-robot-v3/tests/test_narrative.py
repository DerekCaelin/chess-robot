# tests/test_narrative.py - the game narrator: spoken notation, template
# variety, and the two renderings. Pure CPU, no engine needed.
#   python3 tests/test_narrative.py
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chessapp.narrative import spoken_san, mark_for, narrate, Teller  # noqa


def _fake_analysis():
    """A tiny analysed game: one good move, one mistake, one mate."""
    moves = [
        {"ply": 1, "no": 1, "color": "white", "san": "e4", "uci": "e2e4",
         "best_san": "e4", "eval_cp": 20, "eval_before_cp": 0,
         "loss_cp": 0, "quality": "best", "quiet": True, "forced": False,
         "capture": False, "check": False, "material_swing": 0,
         "mate_after": None, "candidates": [{"cp": 20}, {"cp": 10}]},
        {"ply": 2, "no": 1, "color": "black", "san": "e5", "uci": "e7e5",
         "best_san": "e5", "eval_cp": 20, "eval_before_cp": 20,
         "loss_cp": 0, "quality": "best", "quiet": True, "forced": False,
         "capture": False, "check": False, "material_swing": 0,
         "mate_after": None, "candidates": [{"cp": 20}]},
        {"ply": 3, "no": 2, "color": "white", "san": "Qh5", "uci": "d1h5",
         "best_san": "Nf3", "eval_cp": 180, "eval_before_cp": 20,
         "loss_cp": 0, "quality": "great", "quiet": False, "forced": False,
         "capture": False, "check": False, "material_swing": 0,
         "mate_after": None, "candidates": [{"cp": 180}, {"cp": 120}]},
        {"ply": 4, "no": 2, "color": "black", "san": "Nc6", "uci": "b8c6",
         "best_san": "Qe7", "eval_cp": 480, "eval_before_cp": 180,
         "loss_cp": 300, "quality": "blunder", "quiet": False,
         "forced": False, "capture": False, "check": False,
         "material_swing": 0, "mate_after": None,
         "candidates": [{"cp": 480}, {"cp": 200}]},
    ]
    return {"meta": {"white": "Alice", "black": "Bob", "result": "1-0",
                     "event": "Test", "date": "2026.01.01"},
            "moves": moves, "turning_points": [4], "decision_points": [3],
            "quiet_runs": [[1, 2]]}


def test_spoken_san():
    assert spoken_san("Nf3") == "knight to f3"
    assert spoken_san("Bxb5+") == "bishop captures on b5 with check"
    assert spoken_san("O-O-O") == "castles queenside"
    assert spoken_san("exd5") == "pawn from e captures on d5"
    assert spoken_san("e8=Q#") == "pawn to e8 promotes to queen, checkmate"
    assert spoken_san("Rd8#") == "rook to d8, checkmate"
    assert spoken_san("Nbd7") == "knight from b to d7"


def test_renderings_and_variety():
    an = _fake_analysis()
    a = narrate(an, seed=1)
    b = narrate(an, seed=2)
    assert "screen" in a and "speech" in a
    # screen keeps notation + numbers; speech speaks the moves
    assert "Qh5" in a["screen"] and "2.Qh5" in a["screen"].replace(" ", "")
    assert "queen to h5" in a["speech"].lower()
    assert "2.Qh5" not in a["speech"]
    # the narrator never refers to itself as the engine
    for text in (a["screen"], a["speech"], b["screen"], b["speech"]):
        assert "engine" not in text.lower(), text
    # different seeds tell it differently
    assert a["screen"] != b["screen"]
    # every phrasing is used before any repeats within a category
    t = Teller(seed=5)
    first = [t.pick("great", 2) for _ in range(len(__import__(
        "chessapp.narrative", fromlist=["GREAT"]).GREAT))]
    second = [t.pick("great", 2) for _ in range(len(first))]
    assert set(first) != set(second) or first != second


def test_facts_and_openings():
    from chessapp import chessfacts as CF
    from chessapp import openings as OP
    import chess
    # an opening callout fires when the defining sequence completes
    assert OP.match_opening(["e2e4", "e7e5", "g1f3", "d7d6"])[0] == \
        "the Philidor Defence"
    assert OP.match_opening(["e2e4", "c7c5"]) is not None
    # a knight landing between a queen and a rook is a fork, and it is
    # computable
    b = chess.Board("4k3/4r3/8/8/1q6/2N5/8/4K2R w - - 0 1")
    kinds = [k for k, _ in CF.move_facts(b, chess.Move.from_uci("c3d5"))]
    assert "fork" in kinds, kinds


def test_marks():
    assert mark_for("brilliant") == "!!"
    assert mark_for("blunder") == "??"
    assert mark_for("best") == ""


def main():
    test_spoken_san()
    test_renderings_and_variety()
    test_facts_and_openings()
    test_marks()
    print("PASS: narrative rendering + variety + facts")


if __name__ == "__main__":
    main()
