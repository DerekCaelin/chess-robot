# tests/test_move_screen.py - the analysis move screen's contract:
#
#   * the board takes the entire available vertical height of its column
#     (two-column layout: board left, transport + text right)
#   * Begin loads the first move ALREADY PLAYING (autoplay)
#   * pause freezes the auto-advance counter - no timer restarts, and a
#     still-scheduled 'waiting for the voice' tick must not advance either
#     (2026-10-22; the pending-tick leak advanced moves while paused)
#
# Run headless on the desktop (needs a display):  python tests/test_move_screen.py
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402
from chessapp import settings  # noqa: E402
settings.apply()
import ui.app as A  # noqa: E402
import ui.ui_kit as kit  # noqa: E402


def _fake_pack():
    pk = A.AP.AnalysisPack({"moves": ["e2e4", "e7e5"],
                           "player_color": "white", "level": 5,
                           "started": "2026-10-22T10:00", "result": "*"})
    pk.moves = [
        {"ply": 1, "no": 1, "color": "white", "san": "e4", "uci": "e2e4",
         "segments": [("icon", "\u265f"), ("move", "1.e4"),
                      ("text", " A quiet start.")],
         "verdict": "The best move.",
         "speech": "Pawn to e4. A quiet start.", "words": 6,
         "next": "e5 Nf3",
         "next_uci": [], "next_plies": [], "best_uci": ["e7e5"],
         "best_line": {}, "wdl": None, "quality": "good", "best": "e7e5",
         "eval_cp": 20, "eval_before_cp": 20},
        {"ply": 2, "no": 1, "color": "black", "san": "e5", "uci": "e7e5",
         "segments": [("icon", "\u265f"), ("move", "1...e5"),
                      ("text", " Symmetrical.")],
         "verdict": "Solid.",
         "speech": "Pawn to e5. Symmetrical.", "words": 5,
         "next": "Nf3",
         "next_uci": [], "next_plies": [], "best_uci": ["g1f3"],
         "best_line": {}, "wdl": None, "quality": "good", "best": "g1f3",
         "eval_cp": 20, "eval_before_cp": 20}]
    pk.generate = lambda *a, **k: pk.moves
    return pk


def settle(root, n=10):
    for _ in range(n):
        root.update()
        time.sleep(0.02)


def main():
    settings.set("analysis.voice", "none")
    settings.set("analysis.auto_advance", True)
    app = A.App(size="800x480")
    root = app.root
    settle(root, 5)
    assert root.winfo_height() >= 400, "window not realized"
    fails = []

    # ---- autoplay: Begin opens the first move already playing -------------
    app.show(A.AnalysisMoveScreen, pack=_fake_pack(), idx=0, autoplay=True)
    scr = app.current
    settle(root)
    if not scr._playing:
        fails.append("autoplay: first move did not start playing")
    if scr._paused:
        fails.append("autoplay: started paused")
    if scr._timer is None:
        fails.append("autoplay: auto-advance timer not running")

    # ---- the board fills its column's height ------------------------------
    board = scr.board
    if board is None:
        fails.append("board never fitted")
    else:
        col_h = scr._board_col.winfo_height()
        if col_h < 250:
            fails.append("board column only %dpx tall" % col_h)
        # the board takes the ENTIRE available height (8px square grid
        # quantization + the layout paddings aside); a gap means the width
        # budget squeezed it
        if col_h - board.px >= 24:
            fails.append("board leaves %dpx of the %dpx column unused"
                         % (col_h - board.px, col_h))
        if board.px > col_h:
            fails.append("board taller than its column: %d > %d"
                         % (board.px, col_h))

    # ---- the quality verdict sits in the label above the text box --------
    if str(scr.verdict_lbl.cget("text")).strip() != "The best move.":
        fails.append("move screen: verdict label wrong (got %r)"
                     % scr.verdict_lbl.cget("text"))

    # ---- the whole two-column body fits the window ------------------------
    body_req = scr.body.winfo_reqwidth()
    if body_req > root.winfo_width():
        fails.append("body needs %dpx wide but window is %d"
                     % (body_req, root.winfo_width()))

    # ---- pause freezes the auto-advance counter ---------------------------
    scr._pause()
    if scr._playing:
        fails.append("pause: still marked playing")
    if scr._timer is not None:
        fails.append("pause: timer still armed")
    idx = scr.idx
    scr._on_timer()             # a timer that fired just before the pause
    if scr.idx != idx:
        fails.append("pause: _on_timer advanced the move")
    # a pending voice tick scheduled before the pause must be inert too
    scr._pending_tick()
    if scr.idx != idx:
        fails.append("pause: pending voice tick advanced the move")
    # manual stepping still works while paused, but stays manual (no timer)
    scr._next()
    scr._prev()
    if scr.idx != idx:
        fails.append("manual step did not move the move")
    if scr._timer is not None:
        fails.append("pause: manual step re-armed the auto-advance timer")

    # ---- play resumes -----------------------------------------------------
    scr._play()
    if not scr._playing or scr._paused:
        fails.append("play: did not clear the paused state")
    if scr._timer is None:
        fails.append("play: auto-advance timer not restarted")

    # ---- tapping "Engine" pauses the auto-advance (2026-11-06) ----------
    scr._explore_toggle()
    if not scr._exploring:
        fails.append("engine: mode did not open")
    if scr._playing or scr._timer is not None:
        fails.append("engine: auto-advance still running in Engine mode")
    if not scr._paused:
        fails.append("engine: not marked paused")
    scr._explore_toggle()       # back to game - must STAY paused
    if scr._exploring:
        fails.append("engine: mode did not close")
    if scr._playing or scr._timer is not None:
        fails.append("engine: leaving Engine mode resumed the auto-advance")

    # ---- Test alternate (2026-11-06) ------------------------------------
    # the board must open at the CURRENT move's position (not a rewind to
    # before it), the live engine must start immediately, the pre-baked
    # review line must not pose as the live one, and depth reports must
    # actually apply - they used to die in an after() call from the engine
    # thread ('depth...' forever).
    scr._explore_toggle()          # test-alternate lives in Engine mode
    fake_eng = {}

    class FakeEng:
        def __init__(self, **kw):
            pass

        def analyse_live(self, fen, on_info, max_depth=0, stop=None):
            fake_eng["fen"] = fen
            fake_eng["calls"] = fake_eng.get("calls", 0) + 1

        def close(self):
            pass

    _real_engine = A.AN.Engine
    A.AN.Engine = FakeEng
    scr._test_start()
    settle(root)
    if [m.uci() for m in scr._test_board.move_stack] != ["e2e4"]:
        fails.append("test-alt: board is not the current move's position "
                     "(%s)" % [m.uci() for m in scr._test_board.move_stack])
    if fake_eng.get("calls", 0) < 1:
        fails.append("test-alt: no engine run on entry")
    if str(scr.next_lbl.cget("text")).strip():
        fails.append("test-alt: pre-baked next line still shown")
    if "Reading the position" not in scr.txt.get("1.0", "end"):
        fails.append("test-alt: no reading placeholder while waiting")
    # a live depth report applies on the Tk thread
    scr._test_q.put((scr._test_gen, 12,
                     {"cp": 30, "depth": 12, "pv": ["e7e5", "g1f3"]}))
    scr._poll()
    settle(root)
    if "depth 12" not in str(scr.verdict_lbl.cget("text")):
        fails.append("test-alt: depth not shown (got %r)"
                     % scr.verdict_lbl.cget("text"))
    body = scr.txt.get("1.0", "end")
    if "projected line" not in body or "e5" not in body:
        fails.append("test-alt: projected line not rendered (got %r)" % body)
    # a superseded search must never overwrite the current one
    scr._test_q.put((scr._test_gen - 1, 30,
                     {"cp": -500, "depth": 30, "pv": []}))
    scr._poll()
    settle(root)
    if "depth 30" in str(scr.verdict_lbl.cget("text")):
        fails.append("test-alt: stale search report was applied")
    # tapping a move re-runs the engine from the new position
    n0 = fake_eng.get("calls", 0)

    class Ev:
        pass

    tap_sq = scr.board.sq

    def tap(f, r):
        e = Ev()
        e.x = f * tap_sq + 2
        e.y = r * tap_sq + 2
        scr._board_tap(e)
    tap(4, 1)                 # e7 - select the black pawn (black to move)
    tap(4, 3)                 # e5 - play it
    settle(root)
    if [m.uci() for m in scr._test_board.move_stack] != ["e2e4", "e7e5"]:
        fails.append("test-alt: tap did not play e7e5 (%s)"
                     % [m.uci() for m in scr._test_board.move_stack])
    if fake_eng.get("calls", 0) <= n0:
        fails.append("test-alt: no fresh engine run after a move")
    scr._test_reset()
    scr._explore_toggle()
    A.AN.Engine = _real_engine

    # ---- analysis UI (2026-11-06) ---------------------------------------
    # transport: back / play-pause / advance all the SAME size
    hs = [scr.back_btn.winfo_reqheight(), scr.play_btn.winfo_reqheight(),
          scr.next_btn.winfo_reqheight()]
    if max(hs) - min(hs) > 2:
        fails.append("transport heights differ: %s" % hs)
    ws = [scr.back_btn.winfo_reqwidth(), scr.play_btn.winfo_reqwidth(),
          scr.next_btn.winfo_reqwidth()]
    if max(ws) - min(ws) > 2:
        fails.append("transport widths differ: %s" % ws)
    # the engine line label reads 'Engine:' (was 'Next:')
    if not str(scr.next_lbl.cget("text")).startswith("Engine:"):
        fails.append("engine line label wrong (got %r)"
                     % scr.next_lbl.cget("text"))
    # black moves use a black piece icon with a white outline (an IMAGE -
    # the filled glyph vanishes on the dark box); white keeps the glyph
    saved_idx = scr.idx
    scr.idx = 1
    scr._show()
    settle(root)
    if not any(d[0] == "image" for d in scr.txt.dump("1.0", "end")):
        fails.append("black move: no piece icon image")
    scr.idx = saved_idx
    scr._show()
    settle(root)
    if any(d[0] == "image" for d in scr.txt.dump("1.0", "end")):
        fails.append("white move: icon should stay a text glyph")
    # hide analysis: the move text takes over the eval + engine-line rows
    h1 = scr.txt.winfo_height()
    scr._toggle_engine()
    settle(root)
    if scr.verdict_lbl.winfo_ismapped() or scr.next_lbl.winfo_ismapped():
        fails.append("hide analysis: eval/engine-line rows still mapped")
    if scr.txt.winfo_height() <= h1:
        fails.append("hide analysis: the move text did not grow (%d -> %d)"
                     % (h1, scr.txt.winfo_height()))
    scr._toggle_engine()
    settle(root)


    # ---- the play/pause button carries an icon or a visible fallback ------
    try:
        img = scr.play_btn.cget("image")
    except tk.TclError:
        img = ""
    txt = str(scr.play_btn.cget("text"))
    if not img and not txt.strip():
        fails.append("play button shows neither an icon nor a glyph")

    scr._cleanup()

    # ---- Board & Pieces (2026-11-06) ------------------------------------
    # grip heights (left) and per-piece jaw adjusts (right) on the same
    # rows; info button on the board height and on each piece's jaws, none
    # on the piece heights
    scr._cleanup()
    app.show(A.PieceHeightScreen)
    bp = app.current
    settle(root)
    if bp.title_text != "Board & Pieces":
        fails.append("Board & Pieces: title is %r" % bp.title_text)
    if sorted(bp.rows) != ["b", "board", "k", "n", "p", "q", "r"]:
        fails.append("Board & Pieces: height rows %s" % sorted(bp.rows))
    if sorted(bp.jaws) != ["b", "k", "n", "p", "q", "r"]:
        fails.append("Board & Pieces: jaw rows %s" % sorted(bp.jaws))
    for pc in ("p", "r", "n", "b", "q", "k"):
        if (bp.rows[pc].grid_info().get("row")
                != bp.jaws[pc].master.grid_info().get("row")):
            fails.append("Board & Pieces: %s height/jaw not one row" % pc)
    # the sliders save through to the value fields
    def _set_board(v):
        settings.set("arm.board_height", v)
        bp.refresh_labels()
    _set_board(0.7)
    if str(bp.rows["board"].cget("text")) != "0.7":
        fails.append("Board & Pieces: board height did not save")
    _set_board(0.5)

    def _set_jaw(v):
        settings.set("arm.piece_jaws",
                     dict(settings.get("arm.piece_jaws"), n=v))
        bp.refresh_labels()
    _set_jaw(-14)
    if str(bp.jaws["n"].cget("text")) != "-14":
        fails.append("Board & Pieces: jaw adjust did not save")
    _set_jaw(-9)
    # pick height adjust is gone from the settings list
    if any(e[0] == "arm.pickheightadj" for e in A.settings.EDITABLE):
        fails.append("pick height adjust still listed in the settings")

    scr._cleanup()

    # ---- analysis home: one status line per generating stage + the bar ----
    import re
    settings.set("analysis.voice", "piper")
    fake = _fake_pack()
    saved_moves, fake.moves = fake.moves, []     # text still generating
    _real_load, _real_gen = A.AP.load_pack, A.AP.generate_async
    _real_state = A.AP.generation_state
    A.AP.load_pack = lambda: fake
    A.AP.generate_async = lambda *a, **k: None
    state = {"alive": True, "state": "start", "done": 0, "total": 2}
    A.AP.generation_state = lambda *a, **k: dict(state)
    app.show(A.AnalysisHomeScreen)
    home = app.current
    settle(root)
    home._poll()
    settle(root)
    got = str(home.prog_lbl.cget("text"))
    if got != "Building analysis":
        fails.append("home: text stage line wrong (got %r)" % got)
    # the text stage keeps its name; progress lives in the bar as it goes
    state.update(done=1, total=2)
    home._poll()
    settle(root)
    got = str(home.prog_lbl.cget("text"))
    if got != "Building analysis":
        fails.append("home: text stage renamed mid-run (got %r)" % got)
    # Begin stays disabled (and inert) until the text exists - pressing it
    # over an empty pack used to do nothing at all
    if str(home.begin_btn.cget("state")) != "disabled":
        fails.append("home: Begin enabled before the text is ready")
    home._begin()
    settle(root)
    if app.current is not home:
        fails.append("home: Begin opened a move screen over an empty pack")
    fake.moves = saved_moves
    state.update(state="audio", voice="espeak", done=1, total=2)
    home._poll()
    settle(root)
    home._poll()
    settle(root)
    if str(home.begin_btn.cget("state")) == "disabled":
        fails.append("home: Begin still disabled with the text ready")
    got = str(home.prog_lbl.cget("text"))
    if got != "Generating espeak voice":
        fails.append("home: espeak stage line wrong (got %r)" % got)
    state.update(voice="piper", done=2, total=2)
    home._poll()
    settle(root)
    got = str(home.prog_lbl.cget("text"))
    if got != "Generating piper voice":
        fails.append("home: piper stage line wrong (got %r)" % got)
    if not home._bar_on or not home.prog_bar.winfo_ismapped():
        fails.append("home: loading bar hidden while generating")
    state.update(alive=False, done=2)
    home._poll()
    settle(root)
    if home._bar_on or home.prog_bar.winfo_ismapped():
        fails.append("home: loading bar still up after the run finished")
    if str(home.prog_lbl.cget("text")).strip() != "Ready":
        fails.append("home: finished state should say Ready (got %r)"
                     % home.prog_lbl.cget("text"))
    # a fresh run (voice switch) brings the bar back
    state.update(alive=True, state="audio")
    home._poll()
    settle(root)
    if not home._bar_on or not home.prog_bar.winfo_ismapped():
        fails.append("home: bar did not come back for a new run")
    # with voice off there is no audio stage at all
    home.voice_var.set(A.AP.VOICE_LABELS["none"])
    state.update(alive=False)
    home._poll()
    settle(root)
    got = str(home.prog_lbl.cget("text"))
    if "audio" in got or "voice" in got or "files loaded" in got:
        fails.append("home: audio stage shown with voice off (got %r)" % got)
    # ---- "Auto advance" OFF must not auto-advance (2026-11-06) ----------
    # Begin used to pass the ENGINE-ANALYSIS checkbox as the autoplay flag,
    # so unchecking 'Auto advance' did nothing and the review played itself
    # through the whole game.  Pin the wiring, not just the behaviour.
    caught = {}
    saved_show = app.show

    def catch_show(cls, **kw):
        caught.clear()
        caught.update(kw)
    app.show = catch_show
    home.adv_var.set(False)
    home.engine_var.set(True)
    home._open(0)
    if caught.get("autoplay"):
        fails.append("auto-advance OFF: Begin still starts playing")
    if not caught.get("show_engine"):
        fails.append("engine checkbox no longer drives show_engine")
    home.adv_var.set(True)
    home._open(0)
    if not caught.get("autoplay"):
        fails.append("auto-advance ON: Begin does not start playing")
    app.show = saved_show

    # the position-strength graph draws from the LIVE analysis entries -
    # and those are SHARED across pack instances (2026-11-06): the idle
    # prewarm generates on its own instance, and the chart used to read a
    # different instance's empty list - no graph at all
    pkA = A.AP.AnalysisPack(fake.game)
    pkB = A.AP.AnalysisPack(fake.game)
    pkA.live_moves = [{"ply": 1, "eval_cp": 30, "eval_before_cp": 20}]
    if not pkB.live_moves:
        fails.append("chart data is not shared across pack instances")
    fake.live_moves = [{"ply": 1, "eval_cp": 30, "eval_before_cp": 20},
                       {"ply": 2, "eval_cp": -40, "eval_before_cp": 30}]
    home._poll()
    settle(root)
    if len(home.chart.find_all()) < 4:
        fails.append("home: position-strength graph not drawn (%d items)"
                     % len(home.chart.find_all()))

    A.AP.load_pack, A.AP.generate_async = _real_load, _real_gen
    A.AP.generation_state = _real_state
    settings.set("analysis.voice", "none")

    root.destroy()

    if fails:
        print("MOVE SCREEN FAIL:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("PASS: move screen layout + autoplay + pause semantics")


if __name__ == "__main__":
    main()
