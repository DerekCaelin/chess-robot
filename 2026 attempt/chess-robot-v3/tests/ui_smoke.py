# tests/ui_smoke.py - every screen must be VISIBLE at 800x480, not just
# constructed. Guards against the class of bug where a widget exists in the
# tree but pack/grid overflow clips it off-screen (e.g. the Manual-corners
# button once hidden below the Direct-input list).
#
# Run headless on the desktop (needs a display):  python tests/ui_smoke.py
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402
from chessapp import settings  # noqa: E402
settings.apply()
import ui.app as A  # noqa: E402


def mapped_button(scr, text):
    # a tuple = acceptable alternative labels (some buttons retitle with the
    # calibration mode / board state)
    if isinstance(text, tuple):
        return [b for t in text for b in mapped_button(scr, t)]
    found = []

    def walk(w):
        for c in w.winfo_children():
            try:
                txt = (str(c.cget("text")).replace("  \u26A0", "")
                       if isinstance(c, tk.Button) else "")
                if (isinstance(c, tk.Button) and txt == text
                        and c.winfo_ismapped()
                        and c.winfo_height() > 1):
                    found.append(c)
            except tk.TclError:
                pass
            walk(c)
    walk(scr)
    return found


def settle(root, n=10):
    for _ in range(n):
        root.update()
        time.sleep(0.02)


def main():
    app = A.App(size="800x480")
    root = app.root
    settle(root, 5)        # realize the window so heights are real
    Wh = root.winfo_height()
    assert Wh >= 400, "window not realized (h=%d)" % Wh
    fails = []
    def _cam_held():
        import subprocess
        try:
            return subprocess.run(["fuser", "/dev/video0"],
                                  capture_output=True).returncode == 0
        except OSError:
            return False
    held = _cam_held()

    checks = [
        (A.HomeScreen, {}, ["\u25B6  PLAY", "Calibrate", "Settings"]),
        (A.CalibrateScreen, {}, ["Camera", "Board",
                                 "Calibration settings", "Reset all to defaults"]),
        (A.DirectScreen, {}, ["Piece heights", "Manual corners",
                              "Calibration test", "Arm sag"]),
        (A.BoardSizeScreen, {}, []),
        (A.CalibrationTestScreen, {}, ["Go"]),
        (A.PieceHeightScreen, {}, []),
        (A.GfudgeScreen, {}, ["Save", "\u25C0", "\u25B6"]),
        (A.StickoutScreen, {}, ["Save", "\u25B2", "\u25BC", "\u25C0", "\u25B6"]),
        (A.RadscaleScreen, {}, ["Save", "\u25B2", "\u25BC", "\u25C0", "\u25B6"]),
        (A.ReachSettingsScreen, {}, ["Save"]),
        (A.CalibTestScreen, {"key": "arm.pickheightadj"}, ["Save"]),
        (A.CalibTestScreen, {"key": "arm.pickheightreachslope"}, ["Save"]),
        (A.PickHeightSettingsScreen, {}, ["Save", "Piece heights"]),
        (A.SettingsScreen, {}, []),
        (A.NewGameScreen, {}, ["BEGIN"]),
        (A.ArmGrabScreen, {}, ["Start", "Save", "Grab", "+",
                               "\u2212", "\u25B2", "\u25BC", "\u25C0",
                               "\u25B6"]),
        (A.CornerCalibScreen, {}, ["Grab", "Save", "Match height", "+",
                                   "\u2212", "\u25B2",
                                   "\u25BC", "\u25C0", "\u25B6"]),
        (A.ArmSagScreen, {}, ["Save", "\u25B2", "\u25BC"]),
        (A.CalibCameraScreen, {}, ["Start", "Save", "Reset", "Manual"]),
        # (ManualCalibScreen below)
        (A.ManualCalibScreen, {}, ["Save", "OK", "Retry", "+", "\u2212"]),
    ]
    if held:
        checks = [c for c in checks
                  if c[0] not in (A.CalibCameraScreen, A.ManualCalibScreen)]
        print("note: /dev/video0 held by another process - camera "
              "screens skipped")
    Ww = root.winfo_width()
    for cls, kw, buttons in checks:
        app.show(cls, **kw)
        settle(root)
        scr = app.current
        # body must fit within its allocated region (catches clipping)
        if hasattr(scr, "body"):
            req = scr.body.winfo_reqheight()
            got = scr.body.winfo_height()
            if got <= 1:
                settle(root, 15)
                got = scr.body.winfo_height()
            if req > got:
                fails.append("%s body needs %d but has %d"
                             % (cls.__name__, req, got))
            # no content may be wider than the window (catches truncation
            # by forcing wrap, or overflow off the right edge)
            if scr.body.winfo_reqwidth() > Ww + 2:
                fails.append("%s body needs %dpx wide but window is %d"
                             % (cls.__name__, scr.body.winfo_reqwidth(), Ww))
        # instruction text must WRAP and stay fully visible: a tk.Text line box
        # sized at one row hides every wrapped line after the first (Arm grab
        # calibration's guided-test instructions, 2026-10-20).
        instr = getattr(scr, "instr_lbl", None) or getattr(scr, "instr", None)
        if isinstance(instr, tk.Text) and instr.winfo_ismapped():
            n = instr.count("1.0", "end-1c", "displaylines")
            n = n[0] if isinstance(n, tuple) else n
            n = int(n or 1)
            if n > int(instr.cget("height")):
                fails.append("%s: instructions wrap to %d lines but the box "
                             "shows %s (clipped)" % (cls.__name__, n,
                                                     instr.cget("height")))
        # ALL text must fit on screen after wrapping - including the strings
        # screens set at RUNTIME (fit verdicts, round-trip reports, failures).
        # If this fails: simplify the text or reclaim vertical space
        # (2026-10-21).
        try:
            for meth in ("_set_instr", "_refresh"):
                fn = getattr(scr, meth, None)
                if callable(fn):
                    fn()
            if hasattr(scr, "_roundtrip_text"):     # corner: longest runtime text
                scr._log_run = lambda *a, **k: None
                scr.samples = [{"square": s,
                                "X": 6.3 + 6.6 * (s in ("h1", "a1")),
                                "Z": {"a8": 132.0, "h8": 38.0,
                                      "h1": 68.0, "a1": 105.0}[s],
                                "Y": 6.0, "auto_h": s != "h8"}
                               for s in ("a8", "h8", "h1", "a1")]
                fit = A.ap.fit_corner_frame(scr.samples)
                scr.frame_out = {"px": round(fit["px"], 3),
                                 "py": round(fit["py"], 3),
                                 "scale": round(fit["scale"], 4),
                                 "offset": round(fit["offset"], 3),
                                 "a0": round(fit["a0"], 2),
                                 "sign": int(fit.get("sign", 1))}
                scr.corr_out = scr._corr(scr.frame_out)
                scr.instr_lbl.configure(
                    text="Corners recorded. Save switches the arm to mode B "
                         "(corners).\n" + scr._roundtrip_text())
        except Exception as e:
            fails.append("%s text setup crashed: %s" % (cls.__name__, e))
        settle(root, 3)
        req = scr.body.winfo_reqheight()
        got = scr.body.winfo_height()
        if req > got:
            fails.append("%s: text does not fit (body needs %d has %d)"
                         % (cls.__name__, req, got))
        for b in buttons:
            if not mapped_button(scr, b):
                fails.append("%s: '%s' not visible/mapped" % (cls.__name__, b))
    # Info icons are PIL-drawn PNGs (the U+24D8 glyph is missing from DejaVu
    # Sans on the Pi); the assets must load and render as image buttons.
    if A.kit._info_photo() is None or A.kit._info_photo(small=True) is None:
        fails.append("info icon PNG asset failed to load")

    def mapped_image_buttons(scr):
        out = []
        def walk(w):
            for c in w.winfo_children():
                try:
                    if (isinstance(c, tk.Button) and c.winfo_ismapped()
                            and c.winfo_height() > 1
                            and (c.cget("image") or "")):
                        out.append(c)
                except tk.TclError:
                    pass
                walk(c)
        walk(scr)
        return out
    app.show(A.DirectScreen)
    settle(root)
    if not mapped_image_buttons(app.current):
        fails.append("DirectScreen: info icons (image buttons) not mapped")
    # Home PLAY with a saved game must present the resume MODAL (inline
    # panel overlapped the Terminal button, 2026-09-18). Real save backed
    # up and restored byte-for-byte.
    try:
        import shutil
        from chessapp import game_state
        gp = os.path.join(A.ROOT, "data", "game.json")
        bak = None
        if os.path.exists(gp):
            bak = os.path.join("/tmp/opencode", "ui_smoke_game.json")
            os.makedirs("/tmp/opencode", exist_ok=True)   # fresh machine: the
            shutil.copy(gp, bak)                         # scratch dir may be absent
        game_state.save(["e2e4", "g8f6"], "white", 4)
        app.show(A.HomeScreen)
        settle(root)
        app.current._play()
        settle(root)
        if not mapped_button(app.root, "Continue"):
            fails.append("Play: resume modal missing 'Continue'")
        for tl in root.winfo_children():
            if isinstance(tl, tk.Toplevel):
                tl.destroy()
        if bak:
            shutil.copy(bak, gp)
        else:
            game_state.clear()
    except Exception as e:
        fails.append("Play resume modal test crashed: %s" % e)

    # BoardView must actually RENDER (a swallowed mid-draw TclError once
    # left both the game board and the Continue preview half-blank)
    try:
        bvv = A.kit.BoardView(app.current.body, px=400)
        bvv.pack()
        settle(root, 5)
        bvv.draw([list(r) for r in ("rnbqkbnr", "pppppppp", "........",
                                    "........", "........", "........",
                                    "PPPPPPPP", "RNBQKBNR")],
                 last_move=(6, 4, 4, 4))
        root.update()
        if len(bvv.find_all()) < 220:
            fails.append("BoardView.draw rendered only %d items"
                         % len(bvv.find_all()))
    except Exception as e:
        fails.append("BoardView.draw raised: %s" % e)

    # A second Cancel while the first is still parking must be ignored: the
    # double tap used to run park() twice and pop TWO screens (2026-10-21).
    try:
        app.show(A.GfudgeScreen)
        settle(root)
        scr = app.current
        calls = []
        real_back = app.back
        app.back = lambda *a, **k: calls.append(1)
        scr.request_back()
        scr.request_back()
        app.back = real_back
        settle(root)
        if len(calls) != 1:
            fails.append("double cancel: back() ran %d times (want 1)"
                         % len(calls))
    except Exception as e:
        fails.append("cancel debounce test crashed: %s" % e)

    root.destroy()
    if fails:
        print("UI SMOKE FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("UI SMOKE PASS: all screens fit 800x480; key buttons mapped")
    return 0


if __name__ == "__main__":
    import os
    rc = main()
    sys.stdout.flush()
    os._exit(rc)   # Tk finalizing while camera threads
    # wind down aborts the interpreter; the verdict is already printed
