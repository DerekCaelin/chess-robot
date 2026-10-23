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
         "speech": "Pawn to e4. A quiet start.", "words": 6},
        {"ply": 2, "no": 1, "color": "black", "san": "e5", "uci": "e7e5",
         "segments": [("icon", "\u265f"), ("move", "1...e5"),
                      ("text", " Symmetrical.")],
         "verdict": "Solid.",
         "speech": "Pawn to e5. Symmetrical.", "words": 5}]
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
        if col_h < 320:
            fails.append("board column only %dpx tall" % col_h)
        # the board takes the ENTIRE available height (8px square grid
        # quantization aside); a gap means the width budget squeezed it
        if col_h - board.px >= 8:
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

    # ---- the play/pause button carries an icon or a visible fallback ------
    try:
        img = scr.play_btn.cget("image")
    except tk.TclError:
        img = ""
    txt = str(scr.play_btn.cget("text"))
    if not img and not txt.strip():
        fails.append("play button shows neither an icon nor a glyph")

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
    if not got.startswith("Generating text (0/2)"):
        fails.append("home: text stage line wrong (got %r)" % got)
    # the text stage drives the label and the bar as it goes
    state.update(done=1, total=2)
    home._poll()
    settle(root)
    got = str(home.prog_lbl.cget("text"))
    if not got.startswith("Generating text (1/2)"):
        fails.append("home: no text-stage progress in the label (got %r)" % got)
    # Begin stays disabled (and inert) until the text exists - pressing it
    # over an empty pack used to do nothing at all
    if str(home.begin_btn.cget("state")) != "disabled":
        fails.append("home: Begin enabled before the text is ready")
    home._begin()
    settle(root)
    if app.current is not home:
        fails.append("home: Begin opened a move screen over an empty pack")
    fake.moves = saved_moves
    state.update(state="audio", done=1, total=2)
    home._poll()
    settle(root)
    home._poll()
    settle(root)
    if str(home.begin_btn.cget("state")) == "disabled":
        fails.append("home: Begin still disabled with the text ready")
    got = str(home.prog_lbl.cget("text"))
    if not got.startswith("Generating piper audio (1/2)"):
        fails.append("home: audio stage line wrong (got %r)" % got)
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
    if "audio" in got or "files loaded" in got:
        fails.append("home: audio stage shown with voice off (got %r)" % got)
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
