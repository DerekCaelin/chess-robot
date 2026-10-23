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


def test_analysis_pack():
    from chessapp import analysispack as AP
    # the no-voice auto-advance scales with the text: 5 s floor, 10 s ceiling
    assert AP.auto_advance_seconds(3, 0) == 5.0
    assert abs(AP.auto_advance_seconds(25, 0) - 10.0) < 1e-9
    assert 5.0 < AP.auto_advance_seconds(15, 0) < 10.0
    # with sound: the sound plus 2 s
    assert abs(AP.auto_advance_seconds(25, 12.0) - 14.0) < 1e-9
    # a saved game becomes a game with the player's side marked
    g = AP.game_from_state({"moves": ["e2e4", "e7e5"],
                            "player_color": "white",
                            "started": "2026-10-22T10:00", "result": "*"})
    assert g.headers["White"] == "Player" and g.headers["Black"] == "Robot"


def test_marks():
    assert mark_for("brilliant") == "!!"
    assert mark_for("blunder") == "??"
    assert mark_for("best") == ""


def test_concept_notes():
    import chessapp.narrative as N
    teller = Teller(seed=7)
    s = {"color": "white", "Color": "White"}
    # the top mover clears its threshold; the quiet one stays quiet
    e = {"concepts": {"center": 3, "material": 0, "space": 1, "structure": -2},
         "uci": "e2e4", "best": "e2e4"}
    note = N._concept_note(teller, e, s)
    assert note is not None
    assert note[2]["concept"] in ("center", "structure")
    e2 = {"concepts": {"center": 1, "space": 1}, "uci": "e2e4", "best": "e2e4"}
    assert N._concept_note(Teller(seed=7), e2, s) is None
    # harvested human phrasing wins over the built-in banks when present
    old = N._PHRASE_LIB
    N._PHRASE_LIB = {"center": {"+": {"white": ["Human phrasing here."],
                                      "any": []}}}
    try:
        e3 = {"concepts": {"center": 4}, "uci": "e2e4", "best": "e2e4"}
        note = N._concept_note(Teller(seed=7), e3, s)
        assert note[0] == "Human phrasing here.", note
    finally:
        N._PHRASE_LIB = old


def test_pack_stale_sounds():
    """Old sound files describe the OLD text: they must be gone before the
    re-analysis starts, so '#/# voice files loaded' never lies (2026-10-23)."""
    import os
    import tempfile
    from chessapp import analysispack as AP
    from chessapp import analysis as AN
    from chessapp import narrative as NR
    tmp = tempfile.mkdtemp(prefix="ap_test_")
    old_cache, old_an, old_pm = AP.CACHE, AN.analyze_game, NR.per_move
    AP.CACHE = tmp
    try:
        pk = AP.AnalysisPack({"moves": ["e2e4", "e7e5"],
                              "player_color": "white",
                              "started": "2026-10-23T10:00", "result": "*"})
        os.makedirs(pk.dir, exist_ok=True)
        for ply in (1, 2):
            with open(pk.wav_path(ply, "piper"), "w") as f:
                f.write("stale")
        seen = {}

        def fake_analyze(g, depth=18, multipv=3, engine=None, progress=None):
            seen["wavs"] = [x for x in os.listdir(pk.dir)
                            if x.endswith(".wav")]
            if progress:
                progress(1, 2)
            return _fake_analysis()

        AN.analyze_game = fake_analyze
        NR.per_move = lambda an: [
            {"ply": 1, "no": 1, "color": "white", "san": "e4", "uci": "e2e4",
             "segments": [], "speech": "", "words": 0},
            {"ply": 2, "no": 1, "color": "black", "san": "e5", "uci": "e7e5",
             "segments": [], "speech": "", "words": 0}]
        states = []
        pk.generate(voice="none",
                    on_progress=lambda d, n, s: states.append((d, n, s)))
        assert seen.get("wavs") == [], seen
        assert pk.has_text(), "text cache not written"
        # the engine reports its own progress under 'start'; only when the
        # text is complete does the state become 'text'
        assert states[0] == (1, 2, "start"), states
        assert states[-1] == (0, 2, "text"), states
    finally:
        AP.CACHE = old_cache
        AN.analyze_game = old_an
        NR.per_move = old_pm


def main():
    test_spoken_san()
    test_renderings_and_variety()
    test_facts_and_openings()
    test_analysis_pack()
    test_marks()
    test_concept_notes()
    test_pack_stale_sounds()
    print("PASS: narrative rendering + variety + facts")


if __name__ == "__main__":
    main()
