# app.py - the touchscreen front end (tkinter, 800x480, touch-first).
#
# Home ─ PLAY / Calibrate / Settings  (+ power & terminal corner icons)
# Calibrate ─ Camera | Arm (probe→fit→verify) | Calibration settings | Reset
# Play ─ NewGame setup ─ Game screen (board, moves, turn, menu)
#
# Everything visual comes from ui_kit so the look stays consistent. Long
# operations (vision watch, engine, robot move) run on ONE worker thread per
# game and communicate through a queue; tools run as subprocesses through
# toolrun with cancel-and-revert semantics.

import argparse
import hashlib
import json
import math
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from chessapp import settings
settings.apply()

import cv2                                       # noqa: E402
import numpy as np                               # noqa: E402
from PIL import Image, ImageTk                   # noqa: E402
import CBstate                                   # noqa: E402
import camera                                    # noqa: E402
from chessapp import game_state, audio, verify_log, speech as SP
from chessapp.graveyard import piece_name as _piece_name, \
    slot_desc as _slot_desc
from chessapp import levels as LV  # noqa: E402
from chessapp import analysispack as AP  # noqa: E402
from chessapp import analysis as AN  # noqa: E402  (the what-if engine:
#   AN was referenced but NEVER IMPORTED - the test-alternate engine thread
#   died on NameError and its print went to the kiosk's invisible stdout,
#   so the live line/depth silently never existed, 2026-11-06)
from chessapp.game_controller import ChessGame  # noqa: E402
from ui import ui_kit as kit                     # noqa: E402
from ui.toolrun import ToolRun, parse_fit       # noqa: E402
import live_calibrate as LC                      # noqa: E402
import arm_probe as ap                           # noqa: E402
import LEGO_Chess_rpd as RD                      # noqa: E402
from lib_al5_2D_IK import getPulseFromAngle      # noqa: E402
import piece_detect                              # noqa: E402

MODE_FILE = os.path.join(ROOT, "data", ".chess_mode")


def _points_hash():
    try:
        with open(CBstate.mydir + "points.txt", "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def _sq(mv2):
    f = ord(mv2[0]) - 97
    return 8 - int(mv2[1]), f


def _hl(mv4):
    fr, fc = _sq(mv4[0:2])
    tr, tc = _sq(mv4[2:4])
    return (fr, fc, tr, tc)


def _fmt(v):
    if isinstance(v, float):
        return ("%0.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def _over_headline(code, text):
    if code == 1:
        return "YOU WIN!"
    if code == 2:
        return "YOU LOSE!"
    return text or "Game over"


class App:
    def __init__(self, fullscreen=False, size="800x480"):
        self.root = tk.Tk()
        kit.style_root(self.root)
        self.root.title("Chess Robot")
        self.root.resizable(False, False)
        w, h = (int(v) for v in size.split("x"))
        if fullscreen:
            # Bare X (our kiosk runs no window manager) ignores Tk's
            # -fullscreen attribute, leaving the window at its default size
            # in the top-left corner. Set the geometry explicitly; keep the
            # attribute too so a WM (if ever added) still gets the hint.
            self.root.geometry("%dx%d+0+0" % (w, h))
            self.root.attributes("-fullscreen", True)
        else:
            self.root.geometry("%dx%d+60+60" % (w, h))
        # The terminal-output strip is hidden by default (2026-11-08): the
        # screens' own UI carries the same information and the strip
        # competed with every layout for the bottom edge.  Settings >
        # "Show terminal output" brings the raw status stream back.
        self.terminal_bar = tk.Frame(self.root, bg="#181b21")
        self.status_lbl = tk.Label(self.terminal_bar, text="", bg="#181b21",
                                   fg=kit.FG_DIM, font=kit.F_SMALL)
        self.status_lbl.pack(side="left", padx=10, pady=2)
        self.apply_terminal()
        self.history = []          # [(cls, kw)]
        self.current = None
        self._tools = []           # keep ToolRun refs alive
        self._display_asleep = False
        self._last_input = time.time()
        self._busy_reasons = set()   # work that must keep the panel alive
        self._IDLE_SECS = 120      # blank the panel after 2 min idle
        self._setup_powerbtn()
        self.root.bind("<Escape>", lambda e: self.back())
        # Any touch/key resets the idle timer and wakes a sleeping panel.
        for seq in ("<Any-Button-1>", "<Any-Button-3>", "<Any-KeyPress>"):
            self.root.bind_all(seq, self._on_input, add="+")
        self.show(HomeScreen)
        # Force the home screen onto the display before tearing down the
        # boot splash (pi/kiosk.sh keeps it up while the slow 3B import ran),
        # so there is no black gap between splash and home.
        self.root.update_idletasks()
        self.root.update()
        self._kill_splash()
        # idle-time work: generate the last game's analysis audio in the
        # background so it is ready before anyone opens it (2026-10-22)
        self.root.after(3000, lambda: AP.prewarm())
        self.root.after(2000, lambda: SP.warm_all())

    def _kill_splash(self):
        """Close the boot-splash process kiosk.sh started in the background."""
        pidfile = os.path.join(ROOT, "data", ".splash.pid")
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.remove(pidfile)
        except OSError:
            pass

    # ── physical power button (pi/powerbtn.sh writes here) ───────────────────

    def _setup_powerbtn(self):
        self._power_modal = None
        self._count_after = None
        self._fifo_path = os.path.join(ROOT, "data", ".powerbtn.fifo")
        self._fifo_fd = None
        try:
            os.makedirs(os.path.dirname(self._fifo_path), exist_ok=True)
            if not os.path.exists(self._fifo_path):
                os.mkfifo(self._fifo_path)
            self._fifo_fd = os.open(self._fifo_path,
                                    os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self._fifo_fd = None
        if self._fifo_fd is not None:
            self.root.after(600, self._powerbtn_tick)

    def _powerbtn_tick(self):
        try:
            data = os.read(self._fifo_fd, 64)
        except (BlockingIOError, OSError):
            data = b""
        if data and self._power_modal is None:
            self._power_modal = self._power_menu()
        self.root.after(400, self._powerbtn_tick)

    def _power_menu(self, countdown=True):
        """Power dialog shared by the physical button (with a 5s countdown +
        auto-shutdown) and the Home power icon (no timer)."""
        m = kit.Modal(self, "")     # title ("Power") is rendered in the body
                                   # so the countdown can sit above it

        def cancel_timer():
            if self._count_after is not None:
                try:
                    self.root.after_cancel(self._count_after)
                except Exception:
                    pass
                self._count_after = None

        def tick():
            if self._power_modal is not m:   # cancelled / replaced
                return
            self._power_countdown -= 1
            if self._power_countdown <= 0:
                try:
                    m.close()
                except Exception:
                    pass
                self._power_modal = None
                self._count_after = None
                self.poweroff()              # soft shutdown at timer's end
                return
            try:
                self._count_lbl.configure(text=str(self._power_countdown))
            except tk.TclError:
                return
            self._count_after = self.root.after(1000, tick)

        def go(fn):
            def _c():
                cancel_timer()
                m.close()
                self._power_modal = None
                fn()
            return _c

        if countdown:
            self._power_countdown = 5
            self._count_lbl = tk.Label(m.body, text="5", font=kit.F_TITLE,
                                       bg=kit.CARD, fg=kit.FG_DIM)
            self._count_lbl.pack(pady=(12, 2))   # number on top, centred
            self._count_after = self.root.after(1000, tick)
        else:
            self._count_after = None

        tk.Label(m.body, text="Power", font=kit.F_TITLE, bg=kit.CARD,
                 fg=kit.FG).pack(pady=(0, 8))   # word below the number

        kit.big_button(m.body, "Shut down", go(self.poweroff),
                       bg=kit.RED, fg="#fff", width=16,
                       height=2).pack(pady=4)
        kit.big_button(m.body, "Restart", go(self.reboot),
                       bg=kit.ACCENT, width=16,
                       height=2).pack(pady=4)
        kit.big_button(m.body, "Cancel", go(lambda: None),
                       bg=kit.CARD_HI, fg=kit.FG, width=16,
                       height=2).pack(pady=4)
        return m

    def _halt_motion(self):
        cur = self.current
        if hasattr(cur, "abandon"):
            try:
                cur.abandon()
            except Exception:
                pass

    def _display_off(self):
        """Drive the display-power MOSFET pin (GPIO17) low so the panel goes
        dark immediately on power-off/reboot or the idle timeout.  Uses
        pi/display_gpio.sh (which holds-then-exits so the level latches); best
        effort - if it fails the shutdown service still covers it."""
        try:
            subprocess.run(
                [os.path.join(ROOT, "pi", "display_gpio.sh"), "off"],
                timeout=3)
        except Exception:
            pass

    def _display_on(self):
        """Wake the panel (GPIO17 high) - called on a tap while asleep."""
        try:
            subprocess.run(
                [os.path.join(ROOT, "pi", "display_gpio.sh"), "on"],
                timeout=3)
        except Exception:
            pass

    def _on_input(self, _event=None):
        self._last_input = time.time()
        if self._display_asleep:
            self._display_asleep = False
            self._display_on()

    def hold_awake(self, tag):
        """Keep the panel on while long work runs (analysis generation takes
        minutes and touches nothing - the idle blank used to black the screen
        in the middle of it)."""
        self._busy_reasons.add(tag)
        self._on_input()

    def release_awake(self, tag):
        self._busy_reasons.discard(tag)
        self._last_input = time.time()

    def _idle_check(self):
        if (not self._display_asleep and not self._busy_reasons
                and (time.time() - self._last_input) > self._IDLE_SECS):
            self._display_asleep = True
            self._display_off()
        self.root.after(5000, self._idle_check)

    def poweroff(self):
        self._halt_motion()
        self._display_off()
        try:
            subprocess.run(["sudo", "-n", "systemctl", "poweroff"],
                           timeout=5)
        except Exception:
            pass
        self.root.destroy()

    def reboot(self):
        self._halt_motion()
        self._display_off()
        try:
            subprocess.run(["sudo", "-n", "systemctl", "reboot"],
                           timeout=5)
        except Exception:
            pass
        self.root.destroy()

    # ── router ───────────────────────────────────────────────────────────────

    def _present(self, cls, **kw):
        # Build FIRST: a constructor failure used to leave a blank screen
        # because the old screen was already destroyed (2026-10-22).
        s = cls(app=self, **kw)
        if self.current is not None:
            try:
                self.current.destroy()
            except Exception:
                pass
        s.pack(fill="both", expand=True)
        self.current = s
        # every menu is idle time (2026-11-08): keep the game analysis
        # growing in the background so the finished game is ready sooner
        try:
            self.root.after(0, AP.prewarm)
        except tk.TclError:
            pass
        s.on_show()

    def show(self, cls, **kw):
        self.history = [h for h in self.history if h[0] is not cls]
        self.history.append((cls, kw))
        self._present(cls, **kw)

    def back(self):
        if len(self.history) > 1:
            self.history.pop()
            cls, kw = self.history[-1]
            self._present(cls, **kw)

    def toast(self, text):
        self.status_lbl.configure(text=text)

    def apply_terminal(self):
        """Show/hide the terminal-output strip from the current setting."""
        if settings.get("ui.show_terminal", False):
            self.terminal_bar.pack(fill="x", side="bottom")
        else:
            self.terminal_bar.pack_forget()

    def run(self):
        self._idle_check()
        self.root.mainloop()


# ── home ─────────────────────────────────────────────────────────────────────

def _play_square_images():
    """The PLAY square hero (word only - treatment 4), drawn exactly as the
    approved mock.  The two states differ by LUMINANCE only."""
    from PIL import ImageDraw, ImageFont
    font = ImageFont.truetype(
        os.path.join(ROOT, "ui", "assets", "fonts",
                     "AtkinsonHyperlegible-Bold.ttf"), 52)
    imgs = []
    for bg in (kit.ACCENT, kit.ACCENT_HI):
        img = Image.new("RGB", (192, 192), bg)
        d = ImageDraw.Draw(img)
        tb = d.textbbox((0, 0), "PLAY", font=font)
        d.text((96 - (tb[2] - tb[0]) // 2 - tb[0],
                96 - (tb[3] - tb[1]) // 2 - tb[1]),
               "PLAY", font=font, fill="#1b1e24")
        imgs.append(ImageTk.PhotoImage(img))
    return imgs


class HomeScreen(kit.Screen):
    BACK = False

    def __init__(self, app):
        super().__init__(app=app, title="")
        # Home (2026-11-08, the approved treatment): the chessbot wordmark
        # large on TOP (equal black margin on every side), Calibrate /
        # Settings / Analysis on the LEFT, the PLAY square hero (word only)
        # on the RIGHT - both columns vertically centred on one line - and
        # POWER alone at the bottom-left, out of the columns.  The 80px
        # checker is the home's signature; every working screen stays flat.
        self.canvas = tk.Canvas(self.body, bg=kit.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._draw_checker())
        # the menu: four buttons of ONE type stacked vertically in the left
        # column (2026-11-08) - L size, Power as the ordinary last member -
        # and the whole group CENTRED VERTICALLY (the wordmark is gone; the
        # home is just the actions now)
        menu = tk.Frame(self.body, bg=kit.BG)
        for text, cmd in (("Calibrate", lambda: app.show(CalibrateScreen)),
                          ("Settings", lambda: app.show(SettingsScreen)),
                          ("Analysis", lambda: app.show(AnalysisHomeScreen)),
                          ("Power", self._power)):
            kit.btn(menu, text, cmd, size="l", kind="secondary",
                    width_px=230).pack(pady=10)
        menu.place(x=160, rely=0.5, anchor="w")
        # PLAY square hero: image carries the pixels, the text attr keeps it
        # findable; the pressed state is one luminance step darker
        self._play_img, self._play_img_dn = _play_square_images()
        self.play_btn = tk.Button(
            self.body, image=self._play_img, text="PLAY", command=self._play,
            bg=kit.ACCENT, activebackground=kit.ACCENT_HI,
            relief="flat", bd=0, highlightthickness=0, width=192, height=192)
        self.play_btn.image = self._play_img
        self.play_btn.place(x=484, rely=0.5, anchor="w")
        self.play_btn.bind(
            "<ButtonPress>",
            lambda _e: self.play_btn.configure(image=self._play_img_dn))
        self.play_btn.bind(
            "<ButtonRelease>",
            lambda _e: self.play_btn.configure(image=self._play_img))


    def _draw_checker(self):
        """The home's checker - shared with the modal backdrops (see
        kit.draw_checker).  Home only among the screens."""
        kit.draw_checker(self.canvas)

    def on_show(self):
        # sitting in menus is idle time: pre-generate the analysis audio
        AP.prewarm()

    def _play(self):
        saved = game_state.load()
        if saved and saved.get("moves") and not saved.get("result"):
            self._show_saved(saved)
        else:
            self.app.show(NewGameScreen)

    def _show_saved(self, saved):
        # A modal keeps Home clean: the preview, Continue, New game and Back
        # all live above a dimmed dialog instead of colliding with the
        # Home buttons (inline panel, 2026-09-18, landed under the Terminal
        # button).
        m = kit.Modal(self.app, "Game in progress")
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack()
        from ChessBoard import ChessBoard
        b = ChessBoard()
        for mv in saved["moves"]:
            b.addTextMove(mv)
        bv = kit.BoardView(row, px=232)
        bv.pack(side="left", padx=(0, 14))
        bv.draw(b.getBoard())
        side = tk.Frame(row, bg=kit.CARD)
        side.pack(side="left")
        tk.Label(side, text=game_state.summary(), font=kit.F_BODY,
                 bg=kit.CARD, fg=kit.FG_DIM, justify="left",
                 wraplength=230).pack(anchor="w", pady=(0, 8))
        # stacked = ONE width (2026-11-07); hero first
        kit.btn_stack(side, [
            ("Continue",
             lambda: (m.close(), self.app.show(GameScreen, resume=True)),
             "confirm", "l"),
            ("New game",
             lambda: (m.close(), self.app.show(NewGameScreen)),
             "secondary", "m"),
            ("Back", m.close, "secondary", "m"),
        ])[0].pack(pady=4)

    def _power(self):
        # Same Power menu as the physical button, but without the countdown
        # / timed auto-shutdown (the user is already at the screen).
        self.app._power_modal = self.app._power_menu(countdown=False)

    def _terminal(self):
        def yes():
            open(MODE_FILE, "w").write("terminal")
            self.app.root.destroy()
        kit.confirm(self.app, "Exit to terminal",
                    "Leave the UI and drop to a console. Restart the UI with "
                    "the 'chessui' command.\n\nThe current game is saved.",
                    yes, yes_text="Exit")


# ── calibrate hub ────────────────────────────────────────────────────────────

def tool_session(app, title, script, args=(), on_success=None):
    """Run a calibration tool with a live log + Cancel (reverts state)."""
    m, append = kit.textlog(app, title)
    lines = []
    holder = {}

    def on_line(l):
        lines.append(l)
        try:
            app.root.after(0, lambda: append(l))
        except Exception:
            pass

    def on_done(rc, cancelled):
        def fin():
            try:
                m.close()
            except Exception:
                pass
            app._tools[:] = [t for t in app._tools if t is not holder.get("t")]
            if cancelled:
                app.toast("%s: cancelled - previous state restored" % title)
            elif rc != 0:
                app.toast("%s: failed (exit %s) - state restored" % (title, rc))
            elif on_success:
                on_success("\n".join(lines))
        try:
            app.root.after(0, fin)
        except Exception:
            fin()

    t = ToolRun(script, args)
    holder["t"] = t
    app._tools.append(t)

    def cancel():
        append("cancel requested...")
        t.cancel()
    btn = kit.big_button(m.body, "Cancel (restore previous state)", cancel,
                         bg=kit.RED, fg="#fff", width=34, height=2,
                         font=kit.F_BODY)
    btn.pack(pady=(8, 0))
    t.start(on_line, on_done)
    return t


class CalibrateScreen(kit.Screen):
    SP = 2   # uniform vertical gap between every element (6 buttons + the
             # warning line must fit the 480px screen: at SP=5 the body
             # needed 423 of its 392 px, 2026-11-18)

    def __init__(self, app):
        super().__init__(app=app, title="Calibrate")
        self.app = app
        col = tk.Frame(self.body, bg=kit.BG)
        col.pack(expand=True)

        # 1) Camera first, above the mode switch.
        self.board_btn = kit.big_button(col, "Camera",
                                        lambda: app.show(CalibCameraScreen),
                                        bg=kit.CARD_HI, fg=kit.FG, width=26,
                                        height=2)
        self.board_btn.pack(pady=self.SP)

        # 2) Board calibration - the corner method (mode A / Arm grab is
        #    retired from the UI (2026-10-21): the corner calibration proved
        #    the more successful method).
        self.calib_btn = kit.big_button(col, "Board", self._calib,
                                        bg=kit.CARD_HI, fg=kit.FG, width=26,
                                        height=2)
        self.calib_btn.pack(pady=self.SP)

        # Capture drop (2026-11-18): the box hanging off the bottom of the
        # board where the arm drops captured pieces.
        kit.big_button(col, "Capture drop",
                       lambda: app.show(CaptureDropScreen),
                       bg=kit.CARD_HI, fg=kit.FG, width=26,
                       height=2).pack(pady=self.SP)

        self.warn_lbl = tk.Label(col, font=kit.F_SMALL, bg=kit.BG,
                                 fg=kit.ACCENT, wraplength=300)
        self.warn_lbl.pack(pady=self.SP)
        kit.big_button(col, "Calibration settings",
                       lambda: app.show(DirectScreen),
                       bg=kit.CARD_HI, fg=kit.FG, width=26,
                       height=2).pack(pady=self.SP)
        kit.big_button(col, "Reset all to defaults", self._reset,
                       bg=kit.RED, fg="#fff", width=26,
                       height=2).pack(pady=self.SP)
        # Mode A (Arm grab / parametric) is retired from the UI (2026-10-21):
        # make sure the arm is on mode B so the game and this calibration
        # speak the same board mapping.
        if settings.get("arm.calib_mode", "A") != "B":
            settings.set("arm.calib_mode", "B")

    def _calib(self):
        self.app.show(CornerCalibScreen)

    def on_show(self):
        h = _points_hash()
        saved = settings.get("arm.points_fingerprint")
        if h is None:
            txt = "No camera calibration found - run Camera first."
        elif saved is None:
            txt = "Arm not yet calibrated for this board setup."
        elif settings.get("board.stale"):
            txt = "Detected board movement, re-run Camera calibration."
        else:
            txt = ""
        self.warn_lbl.configure(text=txt)
        board_bad = h is None or bool(settings.get("board.stale"))
        self.board_btn.configure(text="Camera  \u26A0" if board_bad
                                 else "Camera")

    def _reset(self):
        def yes():
            settings.reset_user()
            pts = settings.get("points_file")
            if pts:
                import CBstate as C
                flat = []
                for k in ("A1", "H1", "A8", "H8"):
                    flat += ["%.6f" % pts[k][0], "%.6f" % pts[k][1]]
                with open(C.mydir + "points.txt", "w") as f:
                    f.write(",".join(flat))
            self.app.toast("Restored golden defaults")
        kit.confirm(self.app, "Reset", "Discard ALL calibration and settings "
                    "and restore factory defaults?", yes, danger=True,
                    yes_text="Reset everything")


# ── embedded calibration screens (no extra windows; touch in-canvas) ─────────

class CanvasView:
    """Scaled BGR image blitting onto a canvas + tap -> source-pixel mapping."""

    def __init__(self, parent, w=780, h=330):
        self.canvas = tk.Canvas(parent, width=w, height=h, bg="#000",
                                highlightthickness=0)
        self.img_id = self.canvas.create_image(0, 0, anchor="nw")
        self.w, self.h = w, h
        self.scale = 1.0
        self._photo = None

    def show(self, bgr):
        hh, ww = bgr.shape[:2]
        s = min(self.w / ww, self.h / hh, 1.0)
        disp = cv2.resize(bgr, (int(ww * s), int(hh * s))) if s < 1.0 else bgr
        self.scale = s
        self._photo = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)))
        self.canvas.itemconfigure(self.img_id, image=self._photo)

    def tap(self, x, y):
        return (int(x / self.scale), int(y / self.scale))


class CalibCameraScreen(kit.Screen):
    """Live camera calibration rendered IN the UI: uses the same
    CalibrationEngine (worker detection thread + temporal stabilizer) as the
    CLI; taps on the canvas are the A1 orientation click.  Side column holds
    Save/Cancel/Manual + board/corners status (Instructions lives behind the
    title-bar info icon); one dynamic instruction line under the video."""

    def __init__(self, app):
        super().__init__(app=app, title="Camera calibration", info=self._instr)
        self.app = app
        self.snap = settings.snapshot()
        self.engine = LC.CalibrationEngine()
        self.engine.status_overlay = False
        LC.tlog("calib: screen open", fresh=True)
        self._lock = threading.Lock()
        self._frame = None
        self._stop = False
        self._searching = False       # auto-detect runs only after "Start"
        self._instr_photo = None
        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both")
        self.view = CanvasView(row, w=570, h=288)
        self.view.canvas.pack(side="left")
        self.view.canvas.bind("<Button-1>", self._on_tap)
        side = tk.Frame(row, bg=kit.BG, width=160)
        side.pack(side="left", fill="y", padx=6)
        self.start_btn = kit.big_button(side, "Start", self._start,
                                        bg=kit.GREEN, height=2, width=13)
        self.start_btn.pack(pady=(2, 3))
        kit.big_button(side, "Save", self._save, bg=kit.CARD_HI,
                       fg=kit.FG, height=2, width=13).pack(pady=1)
        kit.big_button(side, "Reset", self._reset, bg=kit.CARD_HI,
                       fg=kit.FG, height=2, width=13).pack(pady=1)
        kit.big_button(side, "Manual", lambda: app.show(ManualCalibScreen),
                       bg=kit.CARD, fg=kit.FG, height=2, width=13).pack(
            pady=1)
        self.lbl_board = tk.Label(side, font=("Atkinson Hyperlegible", 11), bg=kit.BG,
                                  fg=kit.FG_DIM, anchor="w", justify="left",
                                  wraplength=170)
        self.lbl_board.pack(pady=(4, 1))
        self.lbl_corners = tk.Label(side, font=("Atkinson Hyperlegible", 11), bg=kit.BG,
                                    fg=kit.FG_DIM, anchor="w", justify="left",
                                    wraplength=170)
        self.lbl_corners.pack(pady=1)
        self.instr = kit.instr_label(self.body,
                                     font=(kit.F_BODY[0], 15, "bold"))
        self.instr.pack(fill="x", pady=(2, 0))
        threading.Thread(target=self._cam, daemon=True).start()
        self.bind("<Destroy>", self._cleanup, add="+")
        self._pump()

    def _cam(self):
        # Keep trying until the camera shows up (or the screen is closed):
        # the device may be re-plugged / still re-enumerating when the
        # calibration screen first opens.
        cap = None
        while not self._stop:
            camera.lock_camera()
            cap = camera.open_camera()
            if cap is not None:
                break
            LC.tlog("calib: camera open FAILED (held by another process: "
                    "%s) - retrying in 1.5s" % camera._held_by_other())
            time.sleep(1.5)
        if cap is None:
            return          # screen closed; _pump shows "Waiting for camera"
        self._cap = cap
        LC.tlog("calib: camera open ok")
        try:
            # exposure/wb are PINNED by lock_camera, so the warm-up only has
            # to flush stale frames - 20 was seconds of standing around
            # (2026-11-04)
            for _ in range(8):
                cap.read()
            LC.tlog("calib: warmup done")

            # Board detection is EXPENSIVE (seconds on the 3B).  Running it
            # inline in the capture loop froze the preview between
            # detections; run it on its own thread so the feed stays live.
            def detect_loop():
                LC.tlog("calib: detect loop running")
                while not self._stop:
                    if not self._searching:
                        time.sleep(0.2)
                        continue
                    with self._lock:
                        f = None if self._frame is None else self._frame.copy()
                    if f is not None:
                        try:
                            self.engine.detect(f)
                        except Exception as e:
                            print("camera screen detect failed:", e)
                    time.sleep(0.15)   # the seam detector is fast - keep up

            threading.Thread(target=detect_loop, daemon=True).start()
            while not self._stop:
                ok, f = cap.read()
                if not ok:
                    if self._stop:
                        break
                    continue
                with self._lock:
                    self._frame = f
        finally:
            # The READER thread releases the cap: closing it from <Destroy>
            # raced the in-flight cap.read() -> OpenCV V4L2 segfault and a
            # full app restart on Back (2026-09-20).
            camera.close_camera(cap)
            self._cap = None

    def _pump(self):
        if self._stop:
            return
        try:
            with self._lock:
                f = None if self._frame is None else self._frame.copy()
            if f is not None:
                self.view.show(self.engine.overlay(f))
            self._refresh_status(f is not None)
        except tk.TclError:
            return
        self.after(100, self._pump)

    def _refresh_status(self, cam_ready):
        e = self.engine
        if not self._searching:
            self.lbl_board.configure(text="\u2014 Board: idle", fg=kit.FG_DIM)
            self.lbl_corners.configure(text="\u2014 Corners: idle",
                                       fg=kit.FG_DIM)
            self.instr.configure(
                text="Aim the camera at the board, then tap Start to "
                     "auto-detect the corners.", fg=kit.ACCENT)
            return
        corners_ok = e.committed is not None and not e.searching
        board_ok = e.raw_found or corners_ok
        if board_ok:
            self.lbl_board.configure(text="\u2713 Board locked", fg=kit.GREEN)
        else:
            self.lbl_board.configure(text="\u2717 Board not found.",
                                     fg=kit.RED)
        if corners_ok:
            self.lbl_corners.configure(text="\u2713 Corners locked",
                                       fg=kit.GREEN)
        else:
            self.lbl_corners.configure(text="\u2717 Corners not found.",
                                       fg=kit.RED)
        if not cam_ready:
            self.instr.configure(text="Waiting for camera...", fg=kit.FG_DIM)
        elif not board_ok:
            self.instr.configure(
                text="Position the board directly under the camera.",
                fg=kit.ACCENT)
        elif not corners_ok:
            self.instr.configure(
                text="Move the board so that all corners are visible to "
                     "the camera.", fg=kit.ACCENT)
        else:
            self.instr.configure(text='Corners locked. Tap "Save" to '
                                      "preserve these settings.",
                                 fg=kit.GREEN)

    def request_back(self):
        self._cancel()

    def _start(self):
        # Auto-detect/locking only runs once the player starts, so re-entering
        # this screen can't clobber a just-completed manual corner set.
        LC.tlog("calib: Start tapped")
        self.engine.reset()
        self._searching = True
        try:
            self.start_btn.configure(state="disabled", bg=kit.CARD,
                                     fg=kit.FG_DIM)
        except tk.TclError:
            pass

    INSTR_TEXT = (
        "To understand the state of the game, the app must know the "
        "location of all four corners.  Place the chess board so it is "
        "in full view of the camera.  The app will attempt to locate "
        "the corners automatically.  Calibration is complete when all "
        "four corners are clearly labeled.  For best results, the board "
        "should be empty and stand out against its background.")

    def _instr(self):
        m = kit.Modal(self.app, "Instructions")
        tk.Label(m.body, text=self.INSTR_TEXT, font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=540, justify="left").pack(anchor="w")
        if self._instr_image():
            tk.Label(m.body, image=self._instr_photo, bg=kit.CARD).pack(
                pady=8)
        kit.big_button(m.body, "Back", m.close, bg=kit.CARD_HI, fg=kit.FG,
                       width=12, height=2).pack(pady=4)

    def _instr_image(self):
        if self._instr_photo is not None:
            return True
        try:
            asset = os.path.join(ROOT, "ui", "assets", "camera_locked.png")
            shown = None
            if os.path.exists(asset):
                img = cv2.imread(asset)
                if img is not None:
                    shown = img
            if shown is None:      # fallback: test case + its ground-truth corners
                cap = cv2.imread(os.path.join(ROOT, "tests", "cases",
                                               "setup_center_3_wood_board_b",
                                               "capture.jpg"))
                with open(os.path.join(ROOT, "tests", "cases",
                                       "setup_center_3_wood_board_b",
                                       "corners.json")) as f:
                    corners = json.load(f)
                if cap is None:
                    return False
                shown = LC._draw_overlay(cap, corners, None, False,
                                         status=False)
            sh, sw = shown.shape[:2]
            s = min(360.0 / sw, 200.0 / sh)
            shown = cv2.resize(shown, (int(sw * s), int(sh * s)))
            self._instr_photo = ImageTk.PhotoImage(
                Image.fromarray(cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)))
            return True
        except Exception:
            return False

    def _on_tap(self, evt):
        pass   # corner names follow POSITION (camera is mounted white-side:
               # bottom-left of the picture is A1, always) - nothing to tap

    def _save(self):
        if self.engine.committed is None:
            self.app.toast("No stable detection yet - nothing saved")
            return
        LC._write_points_txt(self.engine.committed)
        # the board is LOCKED from here on out (2026-11-05): keep the mapping
        # as the default the arm calibration goes to, so a missing
        # points.txt restores the board as it is now
        try:
            vals = [float(v) for v in
                    open(CBstate.mydir + "points.txt").read().rstrip()
                    .split(",")]
            settings.set("points_file", {
                "A1": (vals[0], vals[1]), "H1": (vals[2], vals[3]),
                "A8": (vals[4], vals[5]), "H8": (vals[6], vals[7])})
        except Exception as e:
            print("calibration: could not save the default mapping:", e)

        def after():
            settings.set("board.stale", 0)
            self.app.toast("Camera corners saved")
            self.app.back()
        after()

    def _reset(self):
        # The engine latched corners while the board was still being nudged
        # into place - forget them and re-detect onto the current position.
        self.engine.reset()
        self.app.toast("Cleared - re-locking onto the board's new position")

    def _cancel(self):
        if not self.begin_close():
            return
        settings.restore(self.snap)
        self.app.toast("Camera calibration cancelled - previous state restored")
        self.app.back()

    def _cleanup(self, *_):
        # Only signal the worker: the _cam thread's finally releases the
        # cap (a close here raced cap.read() -> OpenCV V4L2 segfault and a
        # full app restart on Back, 2026-09-20). The loop exits within one
        # read (~30 ms) so the device is freed promptly anyway.
        self._stop = True


def _points_vals():
    try:
        with open(CBstate.mydir + "points.txt") as f:
            return [float(v) for v in f.read().rstrip().split(",")]
    except (OSError, ValueError):
        return None


def check_points_match(img, vals, strict=False):
    """(ok, drift_px): does detect_board(img) agree with points.txt?
    lenient mode passes when the board can't be seen; strict (arm
    calibration entry) requires the board to actually BE there - an arm
    walk driven by phantom corners is worse than none (2026-09-18)."""
    import board_autodetect as ba
    res = ba.detect_board(img)
    if res is None:
        return (False, 0.0) if strict else (True, 0.0)
    gt = {"A1": (vals[0], vals[1]), "H1": (vals[2], vals[3]),
          "A8": (vals[4], vals[5]), "H8": (vals[6], vals[7])}
    drift = piece_detect._geometry_drift(res, gt, img.shape[1], img.shape[0])
    return drift <= piece_detect.DRIFT_LIMIT, drift


# CONVENTION (final, 2026-09-18): the camera is mounted on the WHITE side,
# so the raw picture already IS white's perspective: the bottom-left corner
# of any detection is A1 (enforced by live_calibrate.name_by_position).
# points.txt stores raw-pixel corners under those names; no display or tap
# rotations exist anywhere in the pipeline.


def _board_image(sq, zoom, size):
    """Draw the board (or a zoomed 3x3 window centred on the target) into a
    size x size image.  Returns (img, f0, r0, span) where (f0, r0) is the
    view's top-left in board units (files 0..8 left->right, ranks-from-top
    0..8) and span its width in squares - taps are mapped back to gu/gv with
    gu = f0 + (x/size)*span, so a zoomed tap stays a correct board coordinate.
    Orientation matches ap._overlay: a-file left, rank 8 at the top."""
    f = "abcdefgh".index(sq[0])
    r = int(sq[1])
    rr = 8 - r                    # rank-from-top index of the target
    if zoom:
        f0, r0, span = f - 1, rr - 1, 3
    else:
        f0, r0, span = 0, 0, 8
    step = size / float(span)
    light = (0xB8, 0xD9, 0xE8)   # BGR of kit.SQUARE_L #e8d9b8
    dark = (0x4E, 0x6A, 0x7D)    # BGR of kit.SQUARE_D #7d6a4e
    blank = (0x30, 0x27, 0x23)   # BGR of kit.BG #232730 (off-board cells)
    img = np.zeros((size, size, 3), np.uint8)
    img[:] = blank
    for fr in range(8):
        for rr2 in range(8):
            gx = fr - f0
            gy = rr2 - r0
            if gx < 0 or gx >= span or gy < 0 or gy >= span:
                continue
            x0, y0 = int(gx * step), int(gy * step)
            x1, y1 = int((gx + 1) * step), int((gy + 1) * step)
            img[y0:y1, x0:x1] = light if (fr + rr2) % 2 == 0 else dark
    tx0, ty0 = int((f - f0) * step), int((rr - r0) * step)
    tx1, ty1 = int((f + 1 - f0) * step), int((rr + 1 - r0) * step)
    cv2.rectangle(img, (tx0, ty0), (tx1, ty1), (0, 0, 255), 3)
    return img, f0, r0, span


class ArmGrabScreen(kit.Screen):
    """'Arm grab' calibration: no camera / no tapping-on-photos.  The user
    places a pawn on a known square, JOGS the arm (base rotation + reach on a
    semicircle dial, height on a vertical bar) until the closed jaws sit on
    the piece, and presses Grab.  The commanded pose that worked is recorded;
    after 5 grabs fit_grab() solves radscale/stickout/gfudge and the pick-depth
    constant + reach slope directly from geometry (the serial link is
    write-only, but the recorded
    pose IS the model's commanded input)."""

    # Near corners + centre + BOTH far corners: spans all four quadrants, both
    # lateral edges and the reach extremes, so radscale/stickout (reach) and
    # gfudge (angle) are anchored at the worst cases - not just a tight band -
    # giving a much better first candidate for the verify loop.
    # Phased guided "set", then "verify". Each phase collects 2 focused jogs and
    # solves ONLY its own constant(s), so nothing is blended into an ill-
    # conditioned joint fit (this is what makes the height finally converge):
    #   angle  : nudge base (LEFT/RIGHT) to centre      -> gfudge
    #   radial : nudge reach (UP/DOWN) on near+far      -> radscale + stickout
    #   height : nudge HEIGHT bar on near+far           -> pickheightadj + slope
    # Squares give each phase the variation it needs: wide lateral for angle, a
    # near (d8) + far (d1) reach for radial/height so the slope is resolvable.
    PHASES = ("angle", "radial", "height")
    PHASE_SQ = {"angle": ("a8", "h8"),
                "radial": ("d8", "d1"),
                "height": ("d8", "d1")}
    PHASE_HELP = {
        "angle": "nudge \u25C0 \u25B6 (base) until the jaws are dead-centre over "
                 "the pawn",
        "radial": "nudge \u25B2 \u25BC (reach) until the jaws sit over the pawn",
        "height": "use the HEIGHT bar until the jaws grip the pawn",
    }
    X_LO, X_HI = 4.5, 14.66    # commanded reach X (in) - X_HI = sqrt((A+B)^2 -
                               # Y_LO^2), the true wrist reach at pick (low)
                               # height, so the far corners are commandable
    Z_LO, Z_HI = 20.0, 160.0   # commanded base angle Z (deg) - wide enough that
                               # the near-rank edge squares (h8 ~28-37, a8 ~140)
                               # are still jogable on a 1" OR 1.5" board
    Y_LO, Y_HI = 1.6, 7.5      # commanded work height Y (in) - floor sits a
                               # touch above the board so a pawn can be gripped
    BAR_W, BAR_H = 64, 200
    PAD = 14
    PIECE = "pawn"
    ZOOM_LEVELS = (1.0, 5.0, 10.0, 20.0)   # jog-precision levels (1..4); + finer
    JOG_REACH = 1.8    # in - one arrow nudge of reach at 1x (÷precision level)
    JOG_ANGLE = 9.0    # deg - one arrow nudge of base rotation at 1x (÷level)

    def __init__(self, app):
        super().__init__(app=app, title="Arm grab calibration")
        self.app = app
        self.snap = settings.snapshot()
        self.q = queue.Queue()
        self.recs = []
        self.busy = False
        self.stop = False
        self._live = False
        # ── phased guided set + verify-loop state (see chessapp/verify_log.py) ──
        self.phase = "angle"           # angle/radial/height (guided) then verify
        self.phase_i = 0
        self.step_i = 0
        self.phase_samples = []        # samples collected for the current phase
        self.cur_sq = None             # square of the current step
        self.candidate = None          # constants applied in memory right now
        self.candidate_fp = None
        self.seen_fps = set()
        self.validated = set()         # quadrants Good under the CURRENT candidate
        self.session = verify_log.new_session()
        self.refits = 0
        self.MAX_REFITS = 4
        self.MAX_CORRECTIONS = 12
        self.MAX_ATTEMPTS = 20
        self.tall = False                      # optional tall-piece advisory check
        self.cur_piece = "p"
        self._after_park = None
        # start pose for the FIRST step, purely for an un-blank initial paint
        first = self.PHASE_SQ["angle"][0]
        self.pos = [RD.armlength(first), RD.CRUISE_HEIGHT, RD.armangle(first)]
        self.aim = list(self.pos)
        self.origin = list(self.pos)          # model's assumed pose (RED ref)
        self._zoomi = 0                       # index into ZOOM_LEVELS (jog step)

        row = tk.Frame(self.body, bg=kit.BG)
        cols = kit.BoardCols(self.body)
        # LEFT: chess board showing the target square to place the piece on.
        board_col = tk.Frame(cols.board_col, bg=kit.BG)
        board_col.pack()
        self.board = kit.BoardView(board_col, px=192)
        self.board.pack()
        cols.set_board(self.board, redraw=self._draw_board)
        # controls row: everything pressable (jog, height bar, buttons) first
        row = tk.Frame(cols.right, bg=kit.BG)
        row.pack(fill="x")
        # MIDDLE: big directional jog pad (dial removed - the tap was too coarse
        # to be useful). Steps get finer as the zoom increases (below).
        pad_col = tk.Frame(row, bg=kit.BG)
        pad_col.pack(side="left")
        dgrid = tk.Frame(pad_col, bg=kit.BG)
        dgrid.pack()
        self.up_btn = kit.big_button(dgrid, "\u25B2", lambda: self._jog("in"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=3,
                                     height=2, font=kit.F_BTN)
        self.up_btn.grid(row=0, column=1)
        self.left_btn = kit.big_button(dgrid, "\u25C0", lambda: self._jog("cw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=3,
                                       height=2, font=kit.F_BTN)
        self.left_btn.grid(row=1, column=0)
        self.sq_lbl = tk.Label(dgrid, text="\u2014", bg="#181b21", fg=kit.FG,
                               font=("Atkinson Hyperlegible", 20, "bold"), width=3)
        self.sq_lbl.grid(row=1, column=1, sticky="nsew")
        self.right_btn = kit.big_button(dgrid, "\u25B6",
                                       lambda: self._jog("ccw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=3,
                                       height=2, font=kit.F_BTN)
        self.right_btn.grid(row=1, column=2)
        self.down_btn = kit.big_button(dgrid, "\u25BC",
                                       lambda: self._jog("out"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=3,
                                       height=2, font=kit.F_BTN)
        self.down_btn.grid(row=2, column=1)
        zbar = tk.Frame(pad_col, bg=kit.BG)
        zbar.pack(pady=(6, 0))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 6))
        self.zoom_out_btn = kit.big_button(zbar, "\u2212", self._zoom_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=3, height=1, font=kit.F_BTN,
                                           disabled=True)
        self.zoom_out_btn.pack(side="left", padx=(0, 6))
        self.zoom_in_btn = kit.big_button(zbar, "+", self._zoom_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=3, height=1, font=kit.F_BTN)
        self.zoom_in_btn.pack(side="left")

        # RIGHT of the pad: pick/drop HEIGHT bar (tapped to set Y).
        self.bar_c = tk.Canvas(row, width=self.BAR_W, height=self.BAR_H,
                               bg="#181b21", highlightthickness=0)
        self.bar_c.pack(side="left", padx=(6, 0))
        self.bar_c.bind("<Button-1>", self._on_bar)

        side = tk.Frame(row, bg=kit.BG, width=96)
        side.pack(side="left", fill="y", padx=6)
        self.start_btn = kit.big_button(side, "Start", self._start,
                                        bg=kit.GREEN, height=2, width=9)
        self.start_btn.pack(pady=3)
        self.save_btn = kit.big_button(side, "Save", self._save,
                                       bg=kit.CARD_HI, fg=kit.FG,
                                       height=2, width=9, disabled=True)
        self.save_btn.pack(pady=3)
        self.grab_btn = kit.big_button(side, "Grab", self._grab,
                                       bg=kit.ACCENT, height=2, width=9,
                                       disabled=True)
        self.grab_btn.pack(pady=(10, 3))
        # verify-phase controls (hidden during the guided-5 phase)
        self.ready_btn = kit.big_button(side, "Ready", self._attempt,
                                        bg=kit.GREEN, height=2, width=9)
        self.good_btn = kit.big_button(side, "Good", self._judge_good,
                                       bg=kit.GREEN, height=2, width=9)
        self.off_btn = kit.big_button(side, "Wrong", self._judge_off,
                                      bg=kit.RED, height=2, width=9)
        self._side = side                      # parent for phase show/hide
        self._verify_btns = (self.ready_btn, self.good_btn, self.off_btn)

        self.instr_lbl = tk.Text(cols.right, height=1, width=26,
                                 bg=kit.BG, fg=kit.FG, relief="flat",
                                 wrap="word", font=("Atkinson Hyperlegible", 17),
                                 padx=0, pady=0, highlightthickness=0,
                                 cursor="arrow", state="disabled")
        self.instr_lbl.tag_config("b", font=("Atkinson Hyperlegible", 17, "bold"))
        self.instr_lbl.pack(fill="x", pady=(4, 0))
        # The box GROWS to its wrapped height (a one-row box hid every wrapped
        # line after the first); re-fit when the width settles after mapping.
        self.instr_lbl.bind("<Configure>", lambda *_: self._fit_instr())
        self._say("Press Start: the arm will hover over the first square. "
                  "Place a pawn there, then jog the arm onto it and Grab.")

        self._draw_board()
        self._draw_bar()
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _say(self, text, color=None):
        self._say_rich([(text, False)], color)

    def _say_rich(self, parts, color=None):
        """parts = list of (text, bold). Renders into the read-only Text line,
        bolding square/piece tokens so the target stands out."""
        try:
            w = self.instr_lbl
            w.configure(state="normal")
            w.delete("1.0", "end")
            for text, bold in parts:
                w.insert("end", text, ("b",) if bold else ())
            w.configure(state="disabled", foreground=color or kit.FG)
        except tk.TclError:
            return
        self._fit_instr()

    def _fit_instr(self):
        """Size the instruction box to its WRAPPED height so the test
        instructions wrap and stay fully visible (2026-10-20: a height=1 box
        showed only the first line - the rest of each instruction was
        clipped).  The font scales down (2026-11-05) so the block never runs
        outside the window."""
        try:
            w = self.instr_lbl
            avail = kit.avail_h(w) or 140
            n = 1
            for sz in (17, 15, 13, 11, 10):
                cur = str(w.cget("font"))
                w.configure(font=("Atkinson Hyperlegible", sz))
                w.tag_config("b", font=("Atkinson Hyperlegible", sz, "bold"))
                n = w.count("1.0", "end-1c", "displaylines")
                n = n[0] if isinstance(n, tuple) else n
                n = max(1, int(n or 1))
                lh = kit.tkfont.Font(family="Atkinson Hyperlegible",
                                     size=sz).metrics("linespace")
                if n * lh <= avail or sz == 10:
                    break
            if int(w.cget("height")) != n:
                w.configure(height=n)
        except tk.TclError:
            pass

    def _dbg(self, where, exc):
        """Quiet safety net: log the full traceback to data/verify_last.txt and
        show a readable red line, so a regression reports itself instead of
        silently freezing the controls."""
        import traceback
        try:
            verify_log.add_flag(self.session, "crash",
                                "%s: %r\n%s" % (where, exc, traceback.format_exc()))
            verify_log.flush(self.session)
        except Exception:
            pass
        self._say("Something went wrong (%s): %s" % (where, exc), kit.RED)

    def _set_live(self, flag):
        self._live = flag
        try:
            if flag:
                self.grab_btn.configure(state="normal", bg=kit.ACCENT,
                                        fg="#1b1e24")
            else:
                self.grab_btn.configure(state="disabled", bg=kit.CARD,
                                        fg=kit.FG_DIM)
        except tk.TclError:
            pass

    # target-square board view (left of the pad): shows where to place the piece
    def _draw_board(self):
        try:
            board = [["."] * 8 for _ in range(8)]
            sq = self.cur_sq
            if sq:
                col = RD.xtrans[sq[0]]
                row = 8 - int(sq[1])
                board[row][col] = "P"
                self.board.draw(board, last_move=(row, col, row, col))
                self.sq_lbl.configure(text=sq)
            else:
                self.board.draw(board)
                self.sq_lbl.configure(text="\u2014")
        except Exception as e:
            self._dbg("draw_board", e)

    def _zoom_in(self):
        if self._zoomi >= len(self.ZOOM_LEVELS) - 1:
            return
        self._zoomi += 1
        self._update_zoom_btns()

    def _zoom_out(self):
        if self._zoomi <= 0:
            return
        self._zoomi -= 1
        self._update_zoom_btns()

    def _update_zoom_btns(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._zoomi + 1))
            if self._zoomi >= len(self.ZOOM_LEVELS) - 1:
                self.zoom_in_btn.configure(state="disabled", bg=kit.CARD,
                                           fg=kit.FG_DIM)
            else:
                self.zoom_in_btn.configure(state="normal", bg=kit.CARD_HI,
                                           fg=kit.FG)
            if self._zoomi <= 0:
                self.zoom_out_btn.configure(state="disabled", bg=kit.CARD,
                                            fg=kit.FG_DIM)
            else:
                self.zoom_out_btn.configure(state="normal", bg=kit.CARD_HI,
                                            fg=kit.FG)
        except tk.TclError:
            pass

    def _Y_ypix(self, Y):
        return self.PAD + (self.Y_HI - Y) / (self.Y_HI - self.Y_LO) \
            * (self.BAR_H - 2 * self.PAD)

    def _ypix_to_Y(self, y):
        t = (y - self.PAD) / (self.BAR_H - 2 * self.PAD)
        return max(self.Y_LO, min(self.Y_HI, self.Y_HI - t *
                                  (self.Y_HI - self.Y_LO)))

    def _draw_bar(self):
        b = self.bar_c
        b.delete("all")
        b.create_rectangle(14, self.PAD, self.BAR_W - 14, self.BAR_H - self.PAD,
                           fill="#20242c", outline=kit.FG_DIM)
        ch = max(self.PAD, min(self.BAR_H - self.PAD,
                               self._Y_ypix(RD.CRUISE_HEIGHT)))
        b.create_line(14, ch, self.BAR_W - 14, ch, fill=kit.FG_DIM, width=2)
        # RED = the model's original height; white = the tapped/current height
        oy = max(self.PAD, min(self.BAR_H - self.PAD,
                               self._Y_ypix(self.origin[1])))
        b.create_line(14, oy, self.BAR_W - 14, oy, fill=kit.RED, width=2)
        wy = max(self.PAD, min(self.BAR_H - self.PAD, self._Y_ypix(self.aim[1])))
        b.create_line(10, wy, self.BAR_W - 10, wy, fill="#ffffff", width=3)
        b.create_text(self.BAR_W / 2, self.PAD - 4, text="height",
                       fill=kit.FG_DIM, font=kit.F_SMALL)

    # ── input ─────────────────────────────────────────────────────────────────

    def _on_bar(self, evt):
        if not self._live:
            return
        Y = self._ypix_to_Y(evt.y)
        self.aim = [self.aim[0], Y, self.aim[2]]
        self._draw_bar()
        self._dispatch(self.aim[0], Y, self.aim[2])

    def _jog(self, direction):
        # One small step toward/away (reach) or clockwise/counter (base).  The
        # step shrinks as the dial zooms in, so a tap nudges less at 10x.
        if not self._live:
            return
        f = self.ZOOM_LEVELS[self._zoomi]
        dr = self.JOG_REACH / f
        da = self.JOG_ANGLE / f
        X, Z = self.aim[0], self.aim[2]
        if direction == "in":            # up arrow  - reach inward
            X -= dr
        elif direction == "out":         # down arrow - reach outward
            X += dr
        elif direction == "cw":          # left arrow
            Z += da
        elif direction == "ccw":         # right arrow
            Z -= da
        X = max(self.X_LO, min(self.X_HI, X))
        Z = max(self.Z_LO, min(self.Z_HI, Z))
        self.aim = [X, self.aim[1], Z]
        self._dispatch(X, self.aim[1], Z)

    def _start(self):
        if self.busy or self.recs:
            return
        self.busy = True
        self._set_live(False)
        self.start_btn.configure(state="disabled", bg=kit.CARD_HI, fg=kit.FG)

        def run():
            try:
                CBstate.AL5D_pair_travel_joints = False
                RD.STRICT_IK = True
                # Gentle cold wake: power_up() energises ALL joints at once
                # onto the elevated READY pose slowly (power_up() IS the wake -
                # no extra energise()). The first real movement is the test
                # hover in _begin_phase.
                if not RD.power_up():
                    self.q.put(("say", "SSC-32U not found - check USB cable "
                                       "and servo power.", kit.RED))
                    self.q.put(("fatal_start",))
                    return
                self.q.put(("armed",))
            except Exception as e:
                self.q.put(("say", "Arm link lost: %s" % e, kit.RED))
                self.q.put(("fatal_start",))
        threading.Thread(target=run, daemon=True).start()

    def _dispatch(self, X, Y, Z):
        if self.busy:
            return
        self.busy = True
        self._set_live(False)

        def run():
            try:
                RD.targetXYZGWAWR = (float(X), float(Y), float(Z),
                                     RD.targetXYZGWAWR[3],
                                     -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors(speeds=RD.SPEED_TRAVEL, pick_lean=True)
                self.pos = [X, Y, Z]
                self.q.put(("moved",))
            except RD.IKUnreachable:
                self.q.put(("say", "That spot is out of the arm's travel - "
                            "pick a nearer / shallower point.", kit.RED))
                self.q.put(("revert",))
            except Exception as e:
                self.q.put(("say", "Arm move failed: %s" % e, kit.RED))
                self.q.put(("revert",))
        threading.Thread(target=run, daemon=True).start()

    def _grab(self):
        if self.busy or not self._live:
            return
        self.busy = True
        self._set_live(False)
        verify = self.phase == "verify"
        sq = self.cur_sq
        origin = dict(zip(("X", "Y", "Z"), self.origin))
        pose = {"square": sq, "X": self.pos[0], "Y": self.pos[1],
                "Z": self.pos[2]}

        def run():
            try:
                # Close then release the JAWS ONLY - never re-solve the arm IK,
                # so grabbing a far square on the reach limit works.
                RD.set_gripper(RD.grip_width("p"))
                RD.set_gripper(RD.grip_open_width())
                self.q.put(("grab_verify" if verify else "set", pose, origin))
            except Exception as e:
                self.q.put(("say", "Grab failed: %s" % e, kit.RED))
                self.q.put(("moved",))
        threading.Thread(target=run, daemon=True).start()

    def _park_arm(self):
        """Fold the arm back to park (run off the Tk thread; park() is a
        sequenced multi-step move)."""
        if self.busy:
            return
        self.busy = True
        self._set_live(False)

        def run():
            try:
                RD.park()
            except Exception as e:
                self.q.put(("say", "Park failed: %s" % e, kit.RED))
            self.q.put(("parked",))
        threading.Thread(target=run, daemon=True).start()

    def _begin_phase(self):
        self.phase = self.PHASES[self.phase_i]
        self.step_i = 0
        self.phase_samples = []
        self._begin_step()

    def _begin_step(self):
        sqs = self.PHASE_SQ[self.phase]
        self.cur_sq = sqs[self.step_i]
        self._grab_btn_set_text("Set")
        self._draw_board()
        self._hover(self.cur_sq, cap=True)
        n = self.step_i + 1
        total = len(sqs)
        head = "%s %d/%d - place a pawn on " % (self.phase.capitalize(),
                                                self.phase_i + 1, len(self.PHASES))
        self._say_rich([(head, False), (self.cur_sq, True),
                        (" (%d/%d). " % (n, total), False),
                        (self.PHASE_HELP[self.phase], False),
                        (", then Set.", False)], kit.ACCENT)

    def _grab_btn_set_text(self, txt):
        try:
            self.grab_btn.configure(text=txt)
        except tk.TclError:
            pass

    def _commit_phase(self):
        """Solve THIS phase's constant(s) from its 2 samples and apply them in
        memory, so the next phase's auto-hover already uses the updated model.
        The authoritative combined candidate is recomputed at _finish_guided."""
        self._grab_btn_set_text("Grab")
        try:
            if self.phase == "angle":
                g = ap.fit_angle(self.phase_samples)
                if g is None:
                    raise ValueError("no angle samples")
                RD.Gfudge = CBstate.AL5D_Gfudge = float(g)
                note = "base angle (gfudge) set"
            elif self.phase == "radial":
                r = ap.fit_radial(self.phase_samples)
                if r is None:
                    raise ValueError("radial needs two different reaches")
                rs, so = r
                RD.radscale = CBstate.AL5D_radscale = float(rs)
                RD.stickout = CBstate.AL5D_stickout = float(so)
                note = "reach scale set"
            else:  # height
                h = ap.fit_height(self.phase_samples)
                if h is None:
                    raise ValueError("height fit failed")
                adj, slope = h
                RD.pickheightadj = CBstate.AL5D_pickheightadj = float(adj)
                RD.pickheightreachslope = CBstate.AL5D_pickheightreachslope = float(slope)
                RD.extraheight = CBstate.AL5D_extraheight = 0.0
                note = "pick height set"
        except Exception as e:
            self._dbg("commit_" + self.phase, e)
            return
        self.phase_i += 1
        if self.phase_i < len(self.PHASES):
            self._say("%s. Next: %s\u2026" % (note, self.PHASES[self.phase_i]),
                      kit.GREEN)
            self._begin_phase()
        else:
            self._say("%s. Computing calibration\u2026" % note, kit.GREEN)
            self._finish_guided()

    def _hover(self, sq, cap):
        """RED model reference + auto-hover over `sq`.

        cap=True (guided): sit over the square at the highest height its REACH
        allows (cruise_for_reach) rather than clamping reach to fit cruise - on a
        1.5" board the far corners (a1/h1) can only be hovered LOW, and clamping
        the reach made the arm stop short of them. If even the lowest hover is
        out of the annulus, retract to the reach that fits and let the user jog
        the last bit onto the piece.

        cap=False (verify): command the TRUE fitted pose (used to see whether a
        candidate actually lands there)."""
        X = RD.armlength(sq)
        Z = RD.armangle(sq)
        self.origin = [X, RD.CRUISE_HEIGHT, Z]   # uncapped model pose (RED ref)
        min_h = self.Y_LO + 0.4
        Y = RD.cruise_for_reach(X)
        if cap:
            if Y < min_h:                        # too far to hover: retract X to
                X = min(X, RD.reach_at_height(min_h))   # the reach that fits a
                Y = min_h                        # low-but-possible hover height
            X = max(self.X_LO, min(X, self.X_HI))
            Y = max(min_h, min(RD.CRUISE_HEIGHT, Y))
        self.pos = [X, Y, Z]
        self.aim = [X, Y, Z]
        self._zoomi = 0
        self._update_zoom_btns()
        self._draw_bar()
        self._travel(X, Z, sq)

    def _travel(self, X, Z, sq):
        """Guided between-test positioning: raise to cruise at the current reach,
        then rotate + extend via movearm - all PERPENDICULAR and NO wrist lean
        (the lean only appears on the pick descent / fine jog). This replaces the
        old single diagonal _dispatch, which cut a low corner across the board and
        scraped it when going e.g. d4 -> a1. Always emits a terminal event so busy
        cannot get stuck."""
        if self.busy:
            return
        self.busy = True
        self._set_live(False)

        def run():
            try:
                RD.movearm(X, Z,
                           edge_square=sq if RD.is_afile_square(sq) else None)
            except RD.IKUnreachable:
                self.q.put(("say", "That square is out of the arm's reach - jog "
                            "onto the piece manually, then Grab.", kit.RED))
            except Exception as e:
                self.q.put(("say", "Move failed: %s" % e, kit.RED))
            finally:
                self.q.put(("moved",))
        threading.Thread(target=run, daemon=True).start()

    # ── verify loop (see chessapp/verify_log.py) ────────────────────────────

    def _quad(self, sq):
        return ("ad" if sq[0] <= "d" else "eh") + ("58" if int(sq[1]) >= 5
                                                   else "14")

    def _all_quads(self):
        return {"ad58", "ad14", "eh58", "eh14"}

    def _begin_verify(self, fit):
        """Enter the verify phase: apply the fit IN MEMORY only (never settings.set),
        so nothing is on disk until a real Save; then start random-square tests.
        No return to neutral - go straight to the first verify square."""
        self._apply_fit_in_memory(fit)
        self.phase = "verify"
        self.validated = set()
        self.start_btn.pack_forget()
        self.save_btn.configure(state="normal", bg=kit.CARD_HI, fg=kit.FG)
        self._goto_verify_square()

    def _apply_fit_in_memory(self, fit):
        consts = {"radscale": fit["new_radscale"], "stickout": fit["new_stickout"],
                  "gfudge": fit["new_gfudge"],
                  "pickheightadj": fit["new_pickheightadj"],
                  "pickheightreachslope": fit.get("new_pickheightreachslope", 0.0)}
        setattr_map = {
            "radscale": ("radscale", "AL5D_radscale"),
            "stickout": ("stickout", "AL5D_stickout"),
            "gfudge": ("Gfudge", "AL5D_Gfudge"),
            "pickheightadj": ("pickheightadj", "AL5D_pickheightadj"),
            "pickheightreachslope": ("pickheightreachslope", "AL5D_pickheightreachslope"),
        }
        for k, (rd_attr, cb_attr) in setattr_map.items():
            setattr(RD, rd_attr, consts[k])
            setattr(CBstate, cb_attr, consts[k])
        RD.extraheight = 0.0
        CBstate.AL5D_extraheight = 0.0
        self.candidate = consts
        self.candidate_fp = verify_log.add_candidate(self.session, consts,
                                                     fit.get("scatter_in", 0.0),
                                                     len(self.recs))
        self.seen_fps.add(self.candidate_fp)

    def _set_controls(self, show):
        """Exactly one row of phase buttons visible at a time (start/save/grab
        are the persistent column; this manages the verify-only trio)."""
        for b in (self.ready_btn, self.good_btn, self.off_btn):
            b.pack_forget()
        for name in show:
            {"ready": self.ready_btn, "good": self.good_btn,
             "off": self.off_btn}[name].pack(pady=3)

    def _rand_square(self, quad=None):
        if quad:
            files = "abcd" if quad[0] == "a" else "efgh"
            ranks = range(5, 9) if quad[2] == "5" else range(1, 5)
        else:
            files, ranks = "abcdefgh", range(1, 9)
        return random.choice([f + str(r) for f in files for r in ranks])

    def _goto_verify_square(self, retry_sq=None):
        """Move to (or retry) a verify square, then show ONLY 'Ready'. The arm is
        positioned here by a safe reach-aware movearm (NOT the guided fast
        _dispatch), so the user can place the pawn; the attempt is deferred to
        Ready. No return to neutral."""
        if retry_sq is not None:
            sq = retry_sq
        else:
            pend = sorted(self._all_quads() - self.validated)
            sq = self._rand_square(min(pend))
        self.cur_sq, self.cur_piece, self.tall = sq, "p", False
        self._draw_board()
        self._set_live(False)               # grab + arrows inert until JUDGE/jog
        self._set_controls(("ready",))
        done = " / ".join(sorted(self.validated)) or "none"
        self._say_rich([("Verified ", False), (done, False),
                        (".  Positioning over ", False), (sq, True),
                        (" - place a pawn there, then Ready.", False)], kit.ACCENT)
        self._position(sq)

    def _position(self, sq):
        """Safely hover the arm over `sq` at the reach-aware cruise height using
        movearm (rise -> rotate -> extend, normal speed). No energise/hold_ready
        (that swings the base to the side = a 'return to neutral' detour); the
        arm is already awake from the guided phase. Sets self.origin to the
        model's predicted pose and keeps self.pos in sync so the jog dial is
        truthful. Never touches _dispatch, so the guided 'moved' Grab-pop path is
        not triggered here."""
        if self.busy:
            return
        self.busy = True
        self.origin = [RD.armlength(sq), RD.CRUISE_HEIGHT, RD.armangle(sq)]

        def run():
            try:
                RD.movearm(RD.armlength(sq), RD.armangle(sq),
                           edge_square=sq if RD.is_afile_square(sq) else None)
                X = RD.armlength(sq)
                self.pos = [X, RD.cruise_for_reach(X), RD.armangle(sq)]
                self.aim = list(self.pos)
                self.q.put(("pos_done", sq))
            except RD.IKUnreachable:
                self.q.put(("pos_unreach", sq))
            except Exception as e:
                self.q.put(("say", "Position failed: %s" % e, kit.RED))
                self.q.put(("pos_unreach", sq))
        threading.Thread(target=run, daemon=True).start()

    def _enter_jog(self):
        """Wrong / unreachable: enable the jog controls FIRST (so a draw failure
        can't leave the panel dead), then redraw guarded. Grab + arrows + Save all
        live; the player jogs onto the piece and Grabs to add a sample (or Saves
        the current fit, or backs out)."""
        try:
            self.busy = False
            self._set_live(True)            # enables Grab + arrows
            try:
                self.grab_btn.pack_forget()
            except tk.TclError:
                pass
            self.grab_btn.pack(pady=(10, 3))          # guarantee it's visible
            self.save_btn.configure(state="normal", bg=kit.CARD_HI, fg=kit.FG)
            self._set_controls(())            # Ready/Good/Wrong hidden (jogging)
            self.aim = list(self.pos)
            self._draw_board()
            self._draw_bar()
            self._say_rich([("Jog onto the piece on ", False), (self.cur_sq, True),
                            (", then Grab to add a sample (or Save / back out).",
                             False)], kit.ACCENT)
        except Exception as e:
            self._dbg("enter_jog", e)

    def _attempt(self):
        """The arm is already positioned over the square (Ready was pressed after
        the pawn was placed): descend, pinch the jaws closed to judge the landing,
        then ALWAYS reopen and lift back to a safe cruise height (never carries
        the piece). This leaves the arm open + raised so a following 'Wrong' hands
        the jog a safe, truthful pose."""
        if self.busy or not self.cur_sq:
            return
        self.busy = True
        self._set_live(False)
        self._set_controls(())
        pc = self.cur_piece
        predicted = dict(zip(("X", "Y", "Z"), self.origin))

        def run():
            unreachable = False
            sq = self.cur_sq
            X = RD.armlength(sq)
            top = RD.cruise_for_reach(X)
            try:
                RD.moveXY(RD.grip_y(pc, X))
                RD.set_gripper(RD.grip_width(pc))          # pinch, then judge
            except RD.IKUnreachable:
                unreachable = True
            except Exception as e:
                self.q.put(("say", "Attempt failed: %s" % e, kit.RED))
                unreachable = True
            finally:
                # always release the pawn and raise the open gripper to a safe
                # height, even if the descent failed, so we never leave the jaws
                # closed at pick height; and ALWAYS emit the terminal event so
                # 'busy' clears and Good/Wrong always appear.
                try:
                    RD.set_gripper(RD.grip_open_width())
                    if not unreachable:
                        RD.moveXY(top)
                except Exception:
                    pass
                self.pos = [X, top, RD.armangle(sq)]
                self.aim = list(self.pos)
                self.q.put(("attempted", sq, predicted, unreachable))
        threading.Thread(target=run, daemon=True).start()

    def _judge_good(self):
        if self.busy:
            return
        self._set_controls(())
        verify_log.add_test(self.session, self.cur_sq, self._quad(self.cur_sq),
                            self.candidate_fp, None, self.origin, None, "good")
        verify_log.flush(self.session)
        if self.tall:                                   # advisory only
            self._say("Tall piece gripped correctly - nice. Press Save.",
                      kit.GREEN)
            return
        self.validated.add(self._quad(self.cur_sq))
        if self.validated >= self._all_quads():
            self._open_save()
        else:
            self._goto_verify_square()

    def _judge_off(self):
        """Wrong -> reveal the jog dial for this square; the corrected Grab adds a
        sample (see _grab's verify branch). A failed TALL check is advisory only
        (never a geometry sample - it'd corrupt the pawn-based fit)."""
        try:
            self.busy = False                    # never strand a tap behind busy
            self._set_controls(())
            if self.tall:
                verify_log.add_test(self.session, self.cur_sq,
                                    self._quad(self.cur_sq), self.candidate_fp,
                                    None, self.origin, None, "tall-off")
                verify_log.flush(self.session)
                self._say("Tall piece not gripped cleanly - the pick HEIGHT may "
                          "need a nudge. Geometry is verified; Save stays "
                          "available.", kit.RED)
                return
            verify_log.add_test(self.session, self.cur_sq,
                                self._quad(self.cur_sq), self.candidate_fp,
                                None, self.origin, None, "off")
            verify_log.flush(self.session)
            self._enter_jog()
        except Exception as e:
            self._dbg("judge_off", e)

    def _open_save(self):
        """All 4 quadrants proven under the current candidate -> Save unlocks, and
        we OFFER (never require) one optional tall-piece spot-check. No park - the
        arm just moves to the tall-check square."""
        self.save_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")
        self.busy = False
        self.cur_sq = self._rand_square()
        self.cur_piece, self.tall = "q", True
        self._draw_board()
        self._set_live(False)
        self._set_controls(("ready",))
        self._say_rich([("Verified! Optional: place a ", False), ("queen", True),
                        (" on ", False), (self.cur_sq, True),
                        (" + Ready to spot-check pick height. Save is ready.",
                         False)], kit.GREEN)
        self._position(self.cur_sq)

    def _park_then(self, cont):
        if self.busy:
            return
        self.busy = True
        self._set_live(False)

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.q.put(("vpark",))
        self._after_park = cont
        threading.Thread(target=run, daemon=True).start()

    def _converge_note(self, lat_map):
        """Non-blocking model-gap advisory: far-rank and near-rank corrections
        with OPPOSITE lateral sign = a skew the 5 constants can't express."""
        near = [v for q, vs in lat_map.items() if q.endswith("14") for v in vs]
        far = [v for q, vs in lat_map.items() if q.endswith("58") for v in vs]
        if not near or not far:
            return
        if (min(near) < -0.35 and max(far) > 0.35) or \
           (max(near) > 0.35 and min(far) < -0.35):
            msg = ("Error looks directional (near vs far opposite) - a reach x "
                   "angle skew the fit can't remove. Consider adding a skew term; "
                   "or Save the best fit / Cancel.")
            verify_log.add_flag(self.session, "model_gap", msg)
            verify_log.flush(self.session)
            self._say(msg, kit.RED)

    # ── main-thread pump ──────────────────────────────────────────────────────

    def _poll(self):
        self._pump_events()
        if self.winfo_exists():
            self.after(150, self._poll)

    def _pump_events(self):
        try:
            while True:
                ev = self.q.get_nowait()
                k = ev[0]
                if k == "say":
                    self._say(ev[1], ev[2] if len(ev) > 2 else None)
                elif k == "armed":
                    self.busy = False
                    self.phase_i = 0
                    self._begin_phase()
                elif k == "fatal_start":
                    self.busy = False
                    self.start_btn.configure(state="normal", bg=kit.GREEN,
                                             fg="#1b1e24")
                elif k == "moved":
                    self.busy = False
                    self._draw_bar()
                    self._set_live(True)
                elif k == "revert":
                    self.busy = False
                    self.aim = list(self.pos)
                    self._draw_bar()
                    self._set_live(True)
                elif k == "set":                           # guided-phase sample
                    self.busy = False
                    pose = dict(ev[1]); pose["w"] = 1.0
                    self.phase_samples.append(pose)
                    self.recs.append(pose)
                    verify_log.add_guided(self.session, pose)
                    verify_log.flush(self.session)
                    self.step_i += 1
                    if self.step_i < len(self.PHASE_SQ[self.phase]):
                        self._begin_step()
                    else:
                        self._commit_phase()
                elif k == "grab_verify":                   # a correction sample
                    self._record_correction(ev[1], ev[2])
                elif k == "attempted":                     # verify attempt done
                    self.busy = False
                    _, sq, predicted, unreachable = ev
                    verify_log.add_test(self.session, sq, self._quad(sq),
                                        self.candidate_fp, None, predicted,
                                        None, "unreachable" if unreachable
                                        else "pending")
                    verify_log.flush(self.session)
                    # an unreachable reach is a failure - never offer "Good"
                    self._set_controls(("off",) if unreachable
                                       else ("good", "off"))
                    self._say("Did the jaws land on the piece on %s?" % sq
                              if not unreachable else
                              "Could not reach %s - jog onto it and Grab to "
                              "add a sample, or it is a real workspace limit."
                              % sq, kit.ACCENT if not unreachable else kit.RED)
                elif k == "pos_done":                    # verify arm now over sq
                    self.busy = False
                    self._set_live(False)                # still awaiting the pawn
                    self._draw_board()
                    self._draw_bar()
                elif k == "pos_unreach":                # candidate can't reach sq
                    self.busy = False
                    verify_log.add_test(self.session, ev[1],
                                        self._quad(ev[1]), self.candidate_fp,
                                        None, self.origin, None, "unreachable")
                    verify_log.flush(self.session)
                    self._enter_jog()
                elif k == "vpark":
                    self.busy = False
                    cont, self._after_park = getattr(self, "_after_park", None), None
                    if cont:
                        cont()
                elif k == "parked":
                    self.busy = False
        except queue.Empty:
            pass

    # ── verify bookkeeping ────────────────────────────────────────────────────

    def _finish_guided(self):
        fit = ap.fit_grab(self.recs)
        if not fit.get("ok"):
            self._say("Fit failed: %s" % fit.get("reason", "?"), kit.RED)
            return
        self._begin_verify(fit)      # straight to verify; no return to neutral

    def _record_correction(self, pose, predicted):
        """A verify 'Wrong' grab replaces any older sample for the SAME square
        (rigid board -> the newest jog is truth; dedupe so a square never
        double-weights the least-squares fit), then re-fits. The correction is
        weighted 4x for the pick-height regression so a deliberate fix converges
        instead of being outvoted by the guided set."""
        pose = dict(pose)
        pose["w"] = 4.0
        self.recs[:] = [r for r in self.recs if r["square"] != pose["square"]]
        self.recs.append(pose)
        verify_log.add_test(self.session, pose["square"],
                            self._quad(pose["square"]), self.candidate_fp,
                            None, predicted,
                            dict(zip(("X", "Y", "Z"),
                                     (pose["X"], pose["Y"], pose["Z"]))),
                            "corrected")
        verify_log.flush(self.session)
        self.busy = False
        # re-fit on the enlarged set -> a NEW candidate -> must re-prove all 4
        fit = ap.fit_grab(self.recs)
        if not fit.get("ok"):
            self._say("Re-fit failed: %s" % fit.get("reason", "?"), kit.RED)
            return
        self._converge_note(self._lateral_by_quad())
        new_fp = verify_log.fingerprint(
            {"radscale": fit["new_radscale"], "stickout": fit["new_stickout"],
             "gfudge": fit["new_gfudge"],
             "pickheightadj": fit["new_pickheightadj"],
             "pickheightreachslope": fit.get("new_pickheightreachslope", 0.0)})
        self.refits += 1
        self.session["refits"] = self.refits
        if new_fp in self.seen_fps:                       # stall: same config back
            self._terminal("stalled", "Convergence stalled (same fit returned). "
                           "Save the best fit or Cancel.")
            return
        if (self.refits >= self.MAX_REFITS
                or self._corrections() >= self.MAX_CORRECTIONS
                or self.session["attempts"] >= self.MAX_ATTEMPTS):
            self._terminal("budget", "Hit the re-fit/budget cap without full "
                           "coverage. Save the best fit or Cancel.")
            return
        self._apply_fit_in_memory(fit)
        self.validated.clear()                            # reset on re-fit
        self._goto_verify_square(retry_sq=pose["square"])

    def _lateral_by_quad(self):
        out = {}
        for t in self.session["tests"]:
            if t["verdict"] == "corrected" and t.get("err"):
                out.setdefault(t["quadrant"], []).append(t["err"]["lateral_in"])
        return out

    def _corrections(self):
        return sum(1 for t in self.session["tests"]
                   if t["verdict"] == "corrected")

    def _terminal(self, outcome, note):
        verify_log.finalize(self.session, outcome)
        self.save_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")
        self._say(note, kit.RED)
        self.busy = False
        self._set_live(False)
        self._set_controls(())
        self._park_then(None)

    # ── finalise ──────────────────────────────────────────────────────────────

    def _save(self):
        """Commit the CURRENT in-memory candidate (the config that just passed the
        quadrants, or the last re-fit at a stall/budget exit). Until now nothing
        was persisted - this is the only write."""
        cand = self.candidate
        if cand is None:
            fit = ap.fit_grab(self.recs)
            if not fit.get("ok"):
                self._say("Fit failed: %s" % fit.get("reason", "?"), kit.RED)
                return
            cand = {"radscale": fit["new_radscale"],
                    "stickout": fit["new_stickout"],
                    "gfudge": fit["new_gfudge"],
                    "pickheightadj": fit["new_pickheightadj"],
                    "pickheightreachslope": fit.get("new_pickheightreachslope", 0.0)}

        def commit():
            settings.set("arm.radscale", cand["radscale"])
            settings.set("arm.stickout", cand["stickout"])
            settings.set("arm.gfudge", cand["gfudge"])
            settings.set("arm.pickheightadj", cand["pickheightadj"])
            settings.set("arm.pickheightreachslope", cand["pickheightreachslope"])
            # pickheightadj is the FULL pick depth; zero legacy extraheight so
            # apply()'s fold doesn't double it.
            settings.set("arm.extraheight", 0.0)
            verify_log.finalize(self.session, "saved")
            self.app.toast("Calibration constants saved")
            # Park + release torque before leaving (same as Cancel).
            if RD.sp is not None:
                def run():
                    try:
                        RD.park()
                    except Exception:
                        pass
                    self._cleanup()
                    self.app.root.after(0, self.app.back)
                threading.Thread(target=run, daemon=True).start()
            else:
                self.app.back()
        kit.confirm(self.app, "Save calibration?",
                    "Write these constants to the arm?", commit,
                    yes_text="Save")

    def _cancel(self):
        if not self.begin_close():
            return
        self.stop = True
        self._set_live(False)
        verify_log.finalize(self.session, "cancelled")
        settings.restore(self.snap)
        self.app.toast("Arm grab calibration cancelled - previous constants "
                       "restored")
        # Fold the arm back to park, then release + close, THEN go Home - park
        # runs on a worker so the Tk UI doesn't freeze during the move.
        if RD.sp is not None:
            def run():
                try:
                    RD.park()
                except Exception:
                    pass
                self._cleanup()
                self.app.root.after(0, lambda: self.app.show(HomeScreen))
            threading.Thread(target=run, daemon=True).start()
        else:
            self._cleanup()
            self.app.show(HomeScreen)

    def request_back(self):
        self._cancel()

    def _cleanup(self):
        self.stop = True
        try:
            RD.STRICT_IK = False
            CBstate.AL5D_pair_travel_joints = False
            RD.idleall()            # leaving the session -> drop torque (quiet)
            if RD.sp:
                RD.sp.close()
                RD.sp = None
        except Exception:
            pass


class CalibTestScreen(kit.Screen):
    """One-variable calibration test, opened from the ▶ on a Calibration-settings
    row. Each test locks the jog to the SINGLE axis that moves that constant and
    Saves ONLY that setting (via settings.set -> applies -> live in the game), so
    the user gets unambiguous feedback on one knob at a time:
      gfudge        : base ◀ ▶  centre over a square            -> gfudge
      radscale/     : reach ▲ ▼ near + far square              -> radscale+stickout
        stickout
      pickheightadj : HEIGHT bar at the ~PICK_REF reach square -> pickheightadj
      pickheight_   : full jog (base+reach arrows + HEIGHT bar) on d8 + d1
        slope         -> pickheightreachslope; exposes the perceived grip heights
                      for d8/d4/d1 (and shoulder/elbow pulses) to check the
                      "sag below ~1100 pulse" falloff hypothesis.
    """
    axis = {"height": ("pickheightadj",), "full": ("pickheightreachslope",)}
    X_LO, X_HI = 4.5, 14.66
    Z_LO, Z_HI = 20.0, 160.0
    Y_LO, Y_HI = 1.6, 7.5
    BAR_W, BAR_H = 56, 190
    PAD = 14
    # Same jog scale as Arm-grab: precision 1 is deliberately COARSE (9 deg base,
    # 1.8" reach), + zooms 20x by precision 4.
    STEP_REACH = 1.8      # in per ▲▼ tap at precision 1 (÷ level)
    STEP_BASE = 9.0       # deg per ◀▶ tap at precision 1 (÷ level)
    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)   # precision 1..4 (÷ level)

    CFG = {
        "arm.pickheightadj": dict(axis="height", squares=["__ref__"],
            title="Pick height (adjust)",
            help="Place a pawn on %s (mid reach); use the HEIGHT BAR to lower the "
                 "open jaws onto it, then Record & Save."),
        "arm.pickheightreachslope": dict(axis="full", squares=["d8", "d1"],
            title="Pick-height slope",
            help="Place a pawn on %s; centre the jaws (base+reach), lower the "
                 "HEIGHT BAR to grip it, then Record. Do both d8 and d1, then "
                 "Save. d4 height is the pickheightadj reference."),
    }

    def __init__(self, app, key):
        cfg = self.CFG[key]
        super().__init__(app=app, title=cfg["title"])
        self.app = app
        self.key = key
        self.cfg = cfg
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.cur = [8.0, 5.0, 90.0]
        self.samples = []
        self.sidx = 0
        self._prec = 0            # index into PREC_LEVELS (jog step ÷ level)
        self.squares = self._resolve(cfg["squares"])

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=192)
        cols.set_board(self.board, redraw=self._draw_board)

        row = tk.Frame(cols.right, bg=kit.BG)
        row.pack(fill="x")
        pad = tk.Frame(row, bg=kit.BG)
        pad.pack(side="left")
        self._build_jog(pad, cfg["axis"])

        side = tk.Frame(row, bg=kit.BG, width=170)
        side.pack(side="left", fill="y", padx=8)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 10), justify="left")
        self.val_lbl.pack(anchor="w", pady=(0, 6))
        self.rec_btn = kit.big_button(side, "Record", self._record,
                                      bg=kit.ACCENT, height=2, width=10)
        self.rec_btn.pack(pady=3)
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.GREEN,
                                        height=2, width=10, disabled=True)
        self.save_btn.pack(pady=3)

        self.instr_lbl = kit.FitLabel(cols.right, font=("Atkinson Hyperlegible", 16))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._enter()

    # ---- setup helpers ----
    def _resolve(self, squares):
        out = []
        for s in squares:
            out.append(self._ref_square() if s == "__ref__" else s)
        return out

    @staticmethod
    def _ref_square():
        best, bd = "d4", 1e9
        for f in "abcdefgh":
            for r in range(1, 9):
                d = abs(RD.armlength(f + str(r)) - RD.PICK_REF)
                if d < bd:
                    bd, best = d, f + str(r)
        return best

    def _build_jog(self, pad, axis):
        outer = tk.Frame(pad, bg=kit.BG)
        outer.pack()
        g = tk.Frame(outer, bg=kit.BG)
        g.pack(side="left")
        if axis in ("base", "reach", "full"):
            self.up_btn = kit.big_button(g, "\u25B2", lambda: self._jog("in"),
                                         bg=kit.CARD_HI, fg=kit.FG, width=4,
                                         height=2, font=kit.F_BTN)
            self.up_btn.grid(row=0, column=1)
            self.left_btn = kit.big_button(g, "\u25C0", lambda: self._jog("cw"),
                                           bg=kit.CARD_HI, fg=kit.FG, width=4,
                                           height=2, font=kit.F_BTN)
            self.left_btn.grid(row=1, column=0)
            self.dn_btn = kit.big_button(g, "\u25BC", lambda: self._jog("out"),
                                         bg=kit.CARD_HI, fg=kit.FG, width=4,
                                         height=2, font=kit.F_BTN)
            self.dn_btn.grid(row=2, column=1)
            self.rt_btn = kit.big_button(g, "\u25B6", lambda: self._jog("ccw"),
                                         bg=kit.CARD_HI, fg=kit.FG, width=4,
                                         height=2, font=kit.F_BTN)
            self.rt_btn.grid(row=1, column=2)
            # axis lock: gfudge keeps ONLY base (◀▶), reach is disabled there.
            # For the radial/full tests BOTH stay live - ◀▶ recenters (ignored by
            # the radius/height solves) and ▲▼ sets reach.
            if axis == "base":
                def off(b):
                    b.configure(state="disabled", bg=kit.CARD, fg=kit.FG_DIM)
                off(self.up_btn); off(self.dn_btn)
        if axis in ("height", "full"):
            # touch HEIGHT bar (same canvas style as Arm-grab), right of the pad
            self.bar_c = tk.Canvas(outer, width=self.BAR_W, height=self.BAR_H,
                                   bg="#181b21", highlightthickness=0)
            self.bar_c.pack(side="left", padx=(10, 0))
            self.bar_c.bind("<Button-1>", self._on_bar)

        # precision (jog-step) controls on every test screen
        zbar = tk.Frame(pad, bg=kit.BG)
        zbar.pack(pady=(6, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec >= len(self.PREC_LEVELS) - 1:
            return
        self._prec += 1
        self._set_prec()

    def _prec_out(self):
        if self._prec <= 0:
            return
        self._prec -= 1
        self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled", bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    # ---- arm control ----
    def _hover_y(self, X):
        """Base/reach tests hover ~3" so the jaws are visibly over the piece;
        height/full tests start at cruise and the user drives the bar down."""
        if self.cfg["axis"] in ("height", "full"):
            return max(self.Y_LO + 0.4, RD.cruise_for_reach(X))
        return max(self.Y_LO + 0.4, min(3.0, RD.cruise_for_reach(X)))

    def _enter(self):
        sq = self.squares[0]
        self._instr(sq)
        self._draw_board()
        self._refresh_val()
        self._draw_bar()
        self._wake(sq)              # energise ONCE (the real wake) + travel
        self._draw_bar()

    def _wake(self, sq):
        X = RD.armlength(sq); Z = RD.armangle(sq)
        Y = self._hover_y(X)
        self.cur = [X, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                RD.energise()       # the one slow wake, only on screen entry
                RD.movearm(X, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _travel(self, sq):
        """Move to the next sample square (safe travel, no re-energise)."""
        X = RD.armlength(sq); Z = RD.armangle(sq)
        Y = self._hover_y(X)
        self.cur = [X, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                RD.movearm(X, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _instr(self, sq):
        self.instr_lbl.configure(text=self.cfg["help"] % sq)

    def _draw_board(self):
        try:
            board = [["."] * 8 for _ in range(8)]
            sq = self.squares[self.sidx]
            col = RD.xtrans[sq[0]]
            r = 8 - int(sq[1])
            board[r][col] = "P"
            self.board.draw(board, last_move=(r, col, r, col))
        except Exception:
            pass

    def _refresh_val(self):
        def g(k):
            return settings.get("arm." + k, 0.0)
        if self.key == "arm.gfudge":
            # LIVE: gfudge implied by the current base angle vs the model square.
            sq = self.squares[self.sidx]
            live = RD.Gfudge + (self.cur[2] - RD.armangle(sq))
            self.val_lbl.configure(text="gfudge\n%.2f" % live)
        elif self.key in ("arm.radscale", "arm.stickout"):
            self.val_lbl.configure(text="radscale  %.3f\nstickout  %.2f"
                                   % (g("radscale"), g("stickout")))
        elif self.key == "arm.pickheightreachslope":
            self._refresh_slope()
        else:
            name = self.key.split(".")[-1]
            self.val_lbl.configure(text="%s\n%.3f" % (name, g(name)))

    def _ik_pulses(self, X, Y):
        """(shoulder_pulse, elbow_pulse) for a work pose, or (None, None)."""
        try:
            res = RD.al5_2D_IK((X, Y - RD.shoulder_height, 90.0,
                                42 + RD.extrawidth, -90, 90))
            if isinstance(res, tuple):
                return (int(getPulseFromAngle(res[0])),
                        int(getPulseFromAngle(180.0 - res[1])))
        except Exception:
            pass
        return None, None

    def _refresh_slope(self):
        gl = RD.gripper_len
        by = {s["square"]: s for s in self.samples}
        lines = []
        for sq in ("d8", "d4", "d1"):
            X = RD.armlength(sq)
            if sq in by:
                Yw = by[sq]["Y"]
            else:
                Yw = RD.grip_y("p", X)          # projected wrist height
            sp, ep = self._ik_pulses(X, Yw)
            p = "" if sp is None else " S%d E%d" % (sp, ep)
            tag = " ref" if (sq == "d4" and sq not in by) else ""
            lines.append("%s %.2f%s%s" % (sq, Yw - gl, tag, p))
        if "d1" in by and "d8" in by:
            den = by["d1"]["X"] - by["d8"]["X"]
            if abs(den) > 1e-6:
                lines.append("slope %.4f" % ((by["d1"]["Y"] - by["d8"]["Y"])
                                             / den))
            else:
                lines.append("slope --")
        else:
            lines.append("slope --")
        self.val_lbl.configure(text="\n".join(lines))

    # ---- touch height bar ----
    def _Y_ypix(self, Y):
        return self.PAD + (self.Y_HI - Y) / (self.Y_HI - self.Y_LO) \
            * (self.BAR_H - 2 * self.PAD)

    def _ypix_to_Y(self, y):
        t = (y - self.PAD) / (self.BAR_H - 2 * self.PAD)
        return max(self.Y_LO, min(self.Y_HI, self.Y_HI - t *
                                  (self.Y_HI - self.Y_LO)))

    def _draw_bar(self):
        if not hasattr(self, "bar_c"):
            return
        b = self.bar_c
        b.delete("all")
        p = self.PAD
        b.create_rectangle(14, p, self.BAR_W - 14, self.BAR_H - p,
                           fill="#20242c", outline=kit.FG_DIM)
        ch = max(p, min(self.BAR_H - p,
                        self._Y_ypix(RD.cruise_for_reach(self.cur[0]))))
        b.create_line(14, ch, self.BAR_W - 14, ch, fill=kit.FG_DIM, width=2)
        wy = max(p, min(self.BAR_H - p, self._Y_ypix(self.cur[1])))
        b.create_line(10, wy, self.BAR_W - 10, wy, fill="#ffffff", width=3)
        b.create_text(self.BAR_W / 2, p - 4, text="height",
                      fill=kit.FG_DIM, font=kit.F_SMALL)

    def _on_bar(self, evt):
        if self.busy:
            return
        Y = self._ypix_to_Y(evt.y)
        self.cur = [self.cur[0], Y, self.cur[2]]
        self._draw_bar()
        self._refresh_val()
        self._move()

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        X, Y, Z = self.cur
        if direction == "in":
            X -= self.STEP_REACH / lvl
        elif direction == "out":
            X += self.STEP_REACH / lvl
        elif direction == "cw":
            Z += self.STEP_BASE / lvl
        elif direction == "ccw":
            Z -= self.STEP_BASE / lvl
        self.cur = [max(self.X_LO, min(self.X_HI, X)), Y,
                    max(self.Z_LO, min(self.Z_HI, Z))]
        self._draw_bar()
        self._refresh_val()
        self._move()

    def _move(self):
        """Immediate jog: command the current pose directly (no energise, no
        rise-to-cruise) so a tap responds at once."""
        if self.busy:
            return
        self.busy = True
        X, Y, Z = self.cur

        def run():
            try:
                RD.targetXYZGWAWR = (X, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors(pick_lean=True)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    # ---- record / save ----
    def _record(self):
        if self.busy:
            return
        sq = self.squares[self.sidx]
        X, Y, Z = self.cur
        self.samples.append({"square": sq, "X": X, "Y": Y, "Z": Z})
        self._refresh_val()
        if len(self.samples) < len(self.squares):
            self.sidx += 1
            sq2 = self.squares[self.sidx]
            self._instr(sq2)
            self._draw_board()
            self._travel(sq2)
            self._draw_bar()
            self._refresh_val()
            self.save_btn.configure(state="normal", bg=kit.CARD_HI, fg=kit.FG)
        else:
            self.save_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")

    def _save(self):
        try:
            if len(self.samples) < len(self.squares):
                self.samples.append({"square": self.squares[self.sidx],
                                     "X": self.cur[0], "Y": self.cur[1],
                                     "Z": self.cur[2]})
            ph_in = RD.pieceheights["p"]
            if self.key == "arm.gfudge":
                val = ap.fit_angle(self.samples)
                if val is None:
                    raise ValueError("no angle")
                settings.set("arm.gfudge", round(val, 2))
            elif self.key in ("arm.radscale", "arm.stickout"):
                rs = ap.fit_radial(self.samples)
                if rs is None:
                    raise ValueError("need two different reaches")
                settings.set("arm.radscale", round(rs[0], 4))
                settings.set("arm.stickout", round(rs[1], 3))
            elif self.key == "arm.pickheightadj":
                s = self.samples[0]
                adj = s["Y"] - ph_in - RD.gripper_len \
                    - RD.pickheightreachslope * s["X"]
                settings.set("arm.pickheightadj", round(adj, 3))
            else:  # pickheightreachslope
                by = {s["square"]: s for s in self.samples}
                if "d1" not in by or "d8" not in by:
                    raise ValueError("record BOTH d8 (near) and d1 (far)")
                denom = by["d1"]["X"] - by["d8"]["X"]
                if abs(denom) < 0.3:
                    raise ValueError("d1/d8 reach too close for a slope")
                slope = (by["d1"]["Y"] - by["d8"]["Y"]) / denom
                settings.set("arm.pickheightreachslope", round(slope, 4))
            self.app.toast("Saved " + self.key.split(".")[-1])
            self._finish()
        except Exception as e:
            self.instr_lbl.configure(text="Could not save: %s" % e, fg=kit.RED)

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        """Park the arm (park() ends with idleall = torque off), then leave."""
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


class GfudgeScreen(kit.Screen):
    """Gfudge (base angle) calibration: the arm hovers at cruise height over the
    board centre; the player nudges gfudge with \u25C0 \u25B6 until the gripper
    points straight down the rank-4/5 centreline, then Save. Park on Save/Back."""

    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)
    STEP_BASE = 1.0    # deg per ◀▶ tap at precision 1 (÷ level)

    def __init__(self, app):
        super().__init__(app=app, title="Base angle (gfudge)")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self._prec = 0
        self.gfudge = settings.get("arm.gfudge", 0.0)
        self.center_reach = RD.armlength_xy(0.0, 3.5 * RD.squaresize)
        self.hover = max(2.0, RD.cruise_for_reach(self.center_reach) - 2.0)
        self.cur = [self.center_reach, self.hover, self.gfudge + 90.0]

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=192)
        cols.set_board(self.board, redraw=self._draw_board)
        self._draw_board()
        row = tk.Frame(cols.right, bg=kit.BG)
        row.pack(fill="x")
        pad = tk.Frame(row, bg=kit.BG)
        pad.pack(side="left")
        self._build_jog(pad)
        side = tk.Frame(row, bg=kit.BG, width=190)
        side.pack(side="left", fill="y", padx=8)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 18), justify="left")
        self.val_lbl.pack(anchor="w", pady=(0, 6))
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.GREEN,
                                       height=2, width=10)
        self.save_btn.pack(pady=3)

        self.instr_lbl = kit.FitLabel(cols.right, font=("Atkinson Hyperlegible", 16))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._refresh()
        self._go(wake=True)

    def _draw_board(self):
        try:
            board = [["."] * 8 for _ in range(8)]
            for sq in ("d4", "d5", "e4", "e5"):
                col = RD.xtrans[sq[0]]
                r = 8 - int(sq[1])
                board[r][col] = "P"
            self.board.draw(board)
        except Exception:
            pass

    def _build_jog(self, pad):
        g = tk.Frame(pad, bg=kit.BG)
        g.pack()
        self.left_btn = kit.big_button(g, "\u25C0", lambda: self._jog("cw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=4,
                                       height=2, font=kit.F_BTN)
        self.left_btn.grid(row=0, column=0, padx=4)
        self.rt_btn = kit.big_button(g, "\u25B6", lambda: self._jog("ccw"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.rt_btn.grid(row=0, column=1, padx=4)
        zbar = tk.Frame(pad, bg=kit.BG)
        zbar.pack(pady=(6, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec >= len(self.PREC_LEVELS) - 1:
            return
        self._prec += 1
        self._set_prec()

    def _prec_out(self):
        if self._prec <= 0:
            return
        self._prec -= 1
        self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled", bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    def _refresh(self):
        self.val_lbl.configure(text="gfudge\n%.2f\u00b0" % self.gfudge)
        self.instr_lbl.configure(
            text="Swing the arm with \u25C0 \u25B6 until the gripper points "
                 "straight down the line between rank 4 and 5, then Save.")

    def _go(self, wake=False):
        reach = self.center_reach
        Y = self.hover
        Z = self.gfudge + 90.0
        self.cur = [reach, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                if wake:
                    RD.energise()
                RD.movearm(reach, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _rotate(self):
        if self.busy:
            return
        self.busy = True
        reach, Y = self.cur[0], self.cur[1]
        Z = self.gfudge + 90.0
        self.cur = [reach, Y, Z]

        def run():
            try:
                RD.targetXYZGWAWR = (reach, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors()
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        if direction == "cw":
            self.gfudge += self.STEP_BASE / lvl
        elif direction == "ccw":
            self.gfudge -= self.STEP_BASE / lvl
        self.gfudge = max(-30.0, min(30.0, self.gfudge))
        self._refresh()
        self._rotate()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _save(self):
        settings.set("arm.gfudge", round(self.gfudge, 2))
        self.app.toast("Saved gfudge")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


# ── reach settings ───────────────────────────────────────────────────────────

class ReachSettingsScreen(kit.Screen):
    """Manual reach calibration: one screen for reach intercept (stickout) AND
    reach scale (radscale), plus gfudge. A single file (d1-d8, 8 at top); tap a
    square to put a pawn there and move the arm over it at a hover. Sliders tune
    the three planar constants live; Save stores them. Cancel (back) parks and
    returns to Calibration settings with the start values."""

    FILE = "d"
    F_W, CELL = 84, 38
    KEYS = ("stickout", "radscale", "gfudge")
    LABELS = {"stickout": "reach intercept", "radscale": "reach scale",
              "gfudge": "gfudge"}
    RANGES = {"stickout": (-8.0, 8.0), "radscale": (0.3, 2.0),
              "gfudge": (-30.0, 30.0)}
    STEPS = {"stickout": 0.1, "radscale": 0.01, "gfudge": 0.1}
    DEFAULTS = {"stickout": 0.0, "radscale": 1.0, "gfudge": 0.0}

    def __init__(self, app):
        super().__init__(app=app, title="Reach settings")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.sel = None
        self._applied = None
        self.vals = {k: float(settings.get("arm." + k, self.DEFAULTS[k]))
                     for k in self.KEYS}
        self.starts = dict(self.vals)
        self._tuners = {}
        X0, Z0 = self._project(self.FILE + "8")
        self.cur = [X0, self._hover_y(X0), Z0]

        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both", expand=True)
        self.file_c = tk.Canvas(row, width=self.F_W, height=8 * self.CELL,
                                bg="#181b21", highlightthickness=0)
        self.file_c.pack(side="left", padx=(0, 12))
        self.file_c.bind("<Button-1>", self._on_file_tap)
        self._draw_file()

        right = tk.Frame(row, bg=kit.BG)
        right.pack(side="left", fill="both", expand=True, padx=8)
        ctl = tk.Frame(right, bg=kit.BG)
        ctl.pack(fill="x")
        for k in self.KEYS:
            self._build_tuner(ctl, self.LABELS[k], k)
        kit.big_button(ctl, "Save", self._save, bg=kit.GREEN,
                       height=2, width=10).pack(anchor="w", pady=(10, 0))

        self.instr_lbl = kit.FitLabel(right, font=("Atkinson Hyperlegible", 14))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self._set_instr()
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()

    # ---- model (parametric projection with LIVE slider values) ----
    def _project(self, sq):
        bx, by = RD.board_sq_center(sq)
        x = (4.0 - bx) * RD.squaresize
        y = (by - 0.5) * RD.squaresize
        radial = math.hypot(x, y + RD.axistorow0)
        reach = self.vals["radscale"] * (radial + self.vals["stickout"])
        angle = self.vals["gfudge"] + 90.0 + math.degrees(
            math.atan2(x, y + RD.axistorow0))
        return reach, angle

    def _hover_y(self, reach):
        # 0.5 in above the piece; pickheightadj + pickheightreachslope apply.
        return RD.grip_y("p", reach) + 0.5

    def _set_instr(self):
        self.instr_lbl.configure(
            text="Select a square. Place a pawn on that square and adjust "
                 "sliders until the grippers reliably hover above it at any "
                 "distance.")

    # ---- single-file strip ----
    def _draw_file(self):
        c = self.file_c
        c.delete("all")
        for i in range(8):
            rank = 8 - i
            y0 = i * self.CELL
            fill = "#2a3140" if self.sel == rank else "#20242c"
            c.create_rectangle(1, y0 + 1, self.F_W - 1, y0 + self.CELL - 1,
                               fill=fill, outline="#5a4c37")
            c.create_text(12, y0 + self.CELL // 2, text=str(rank),
                          font=("Atkinson Hyperlegible", 11, "bold"), fill=kit.FG_DIM)
            if self.sel == rank:
                cx, cy = self.F_W // 2, y0 + self.CELL // 2
                c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9,
                              fill="#f6f2e8", outline="#16181d")

    def _on_file_tap(self, evt):
        idx = evt.y // self.CELL
        if 0 <= idx < 8:
            self.sel = 8 - idx
            self._draw_file()
            self._go(self.FILE + str(self.sel))

    # ---- tuners ----
    def _build_tuner(self, parent, label, key):
        box = tk.Frame(parent, bg=kit.BG)
        box.pack(fill="x", pady=5)
        top = tk.Frame(box, bg=kit.BG)
        top.pack(fill="x")
        tk.Label(top, text=label, font=("Atkinson Hyperlegible", 12), bg=kit.BG,
                 fg=kit.FG).pack(side="left")
        val_lbl = tk.Label(top, text="", font=("JetBrains Mono", 11),
                           bg=kit.BG, fg=kit.ACCENT, justify="right")
        val_lbl.pack(side="right")
        sr = tk.Frame(box, bg=kit.BG)
        sr.pack(fill="x", pady=(2, 0))
        lo, hi = self.RANGES[key]
        kit.big_button(sr, "\u2212", lambda: self._step(key, -1),
                       bg=kit.CARD, fg=kit.FG, width=3, height=1,
                       font=kit.F_BTN).pack(side="left")
        scale = tk.Scale(sr, from_=lo, to=hi, resolution=self.STEPS[key],
                         orient="horizontal", bg=kit.BG, fg=kit.FG,
                         highlightthickness=0, troughcolor="#20242c",
                         command=lambda v, k=key: self._on_scale(k, float(v)))
        scale.pack(side="left", fill="x", expand=True, padx=4)
        kit.big_button(sr, "+", lambda: self._step(key, +1),
                       bg=kit.CARD_HI, fg=kit.FG, width=3, height=1,
                       font=kit.F_BTN).pack(side="left")
        self._tuners[key] = (val_lbl, scale)
        scale.set(self.vals[key])
        self._refresh_val(key)

    def _refresh_val(self, key):
        self._tuners[key][0].configure(
            text="cur %.3f  start %.3f" % (self.vals[key], self.starts[key]))

    def _on_scale(self, key, v):
        self.vals[key] = v
        self._refresh_val(key)
        self._live()

    def _step(self, key, sign):
        lo, hi = self.RANGES[key]
        v = max(lo, min(hi, self.vals[key] + sign * self.STEPS[key]))
        self._tuners[key][1].set(v)
        self._on_scale(key, v)

    # ---- arm ----
    def _pose(self, sq):
        X, Z = self._project(sq)
        Yw = self._hover_y(X)
        cap = RD.reach_at_height(Yw) - 0.1
        X = max(4.5, min(cap, X))
        Z = max(20.0, min(160.0, Z))
        return X, Yw, Z

    def _go(self, sq):
        X, Yw, Z = self._pose(sq)
        self.cur = [X, Yw, Z]
        if self.busy:
            return
        self.busy = True
        self._applied = list(self.cur)

        def run():
            try:
                RD.energise()
                RD.movearm(X, Z)      # safe travel
                RD.openjaws()
                RD.moveXY(Yw)         # descend to hover, jaws open
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _live(self):
        if self.sel is None:
            return
        self.cur = list(self._pose(self.FILE + str(self.sel)))
        self._apply_pose()

    def _apply_pose(self):
        if self.busy:
            return
        if self._applied is not None and \
                max(abs(a - b) for a, b in zip(self.cur, self._applied)) < 1e-6:
            return
        self.busy = True
        self._applied = list(self.cur)
        X, Yw, Z = self.cur

        def run():
            try:
                RD.targetXYZGWAWR = (X, Yw, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors()
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
                    self._apply_pose()   # apply any pending slider change
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    # ---- save / leave ----
    def _save(self):
        kit.confirm(self.app, "Save",
                    "Store the reach settings?", self._do_save,
                    yes_text="Save")

    def _do_save(self):
        settings.set("arm.stickout", round(self.vals["stickout"], 3))
        settings.set("arm.radscale", round(self.vals["radscale"], 4))
        settings.set("arm.gfudge", round(self.vals["gfudge"], 2))
        self.app.toast("Saved reach settings")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


class StickoutScreen(kit.Screen):
    """Reach intercept (stickout) calibration: a pawn sits on d8 (near); the
    player adjusts stickout (\u25B2\u25BC, radial) and gfudge (\u25C0\u25B6,
    lateral) until the gripper sits over the piece. Park on Save/Back."""

    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)
    STEP_PARAM = 0.1     # stickout in per ▲▼ tap at precision 1
    STEP_GFUDGE = 1.0    # deg per ◀▶ tap at precision 1

    def __init__(self, app):
        super().__init__(app=app, title="Reach intercept (stickout)")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self._prec = 0
        self.stickout = settings.get("arm.stickout", 0.0)
        self.gfudge = settings.get("arm.gfudge", 0.0)
        self.x = 0.5 * RD.squaresize
        self.y = 0.0                       # d8
        self.radial = math.sqrt(self.x * self.x + (self.y + RD.axistorow0) ** 2)
        self.ang_off = 90.0 + math.degrees(
            math.atan(self.x / (self.y + RD.axistorow0)))
        self.hover = max(2.0, RD.cruise_for_reach(self._reach()) - 2.0)
        self.cur = [self._reach(), self.hover, self._angle()]

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=192)
        cols.set_board(self.board, redraw=self._draw_board)
        self._draw_board()
        row = tk.Frame(cols.right, bg=kit.BG)
        row.pack(fill="x")
        pad = tk.Frame(row, bg=kit.BG)
        pad.pack(side="left")
        self._build_jog(pad)
        side = tk.Frame(row, bg=kit.BG, width=190)
        side.pack(side="left", fill="y", padx=8)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 14), justify="left")
        self.val_lbl.pack(anchor="w", pady=(0, 6))
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.GREEN,
                                       height=2, width=10)
        self.save_btn.pack(pady=3)

        self.instr_lbl = kit.FitLabel(cols.right, font=("Atkinson Hyperlegible", 16))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._refresh()
        self._go(wake=True)

    def _reach(self):
        return RD.radscale * (self.radial + self.stickout)

    def _angle(self):
        return self.gfudge + self.ang_off

    def _draw_board(self):
        try:
            board = [["."] * 8 for _ in range(8)]
            col = RD.xtrans["d"]
            r = 8 - 8
            board[r][col] = "P"
            self.board.draw(board, last_move=(r, col, r, col))
        except Exception:
            pass

    def _build_jog(self, pad):
        g = tk.Frame(pad, bg=kit.BG)
        g.pack()
        self.up_btn = kit.big_button(g, "\u25B2", lambda: self._jog("in"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.up_btn.grid(row=0, column=1)
        self.left_btn = kit.big_button(g, "\u25C0", lambda: self._jog("cw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=4,
                                       height=2, font=kit.F_BTN)
        self.left_btn.grid(row=1, column=0)
        self.dn_btn = kit.big_button(g, "\u25BC", lambda: self._jog("out"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.dn_btn.grid(row=2, column=1)
        self.rt_btn = kit.big_button(g, "\u25B6", lambda: self._jog("ccw"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.rt_btn.grid(row=1, column=2)
        zbar = tk.Frame(pad, bg=kit.BG)
        zbar.pack(pady=(6, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec >= len(self.PREC_LEVELS) - 1:
            return
        self._prec += 1
        self._set_prec()

    def _prec_out(self):
        if self._prec <= 0:
            return
        self._prec -= 1
        self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled", bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    def _refresh(self):
        self.val_lbl.configure(text="stickout %.3f\ngfudge %.2f\u00b0"
                               % (self.stickout, self.gfudge))
        self.instr_lbl.configure(
            text="Pawn on d8: \u25B2\u25BC = reach, \u25C0\u25B6 = swing. "
                 "Centre the gripper over the piece, then Save.")

    def _go(self, wake=False):
        reach, Y, Z = self._reach(), self.hover, self._angle()
        self.cur = [reach, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                if wake:
                    RD.energise()
                RD.movearm(reach, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _move(self):
        if self.busy:
            return
        self.busy = True
        reach, Y, Z = self._reach(), self.hover, self._angle()
        self.cur = [reach, Y, Z]

        def run():
            try:
                RD.targetXYZGWAWR = (reach, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors()
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        if direction == "in":
            self.stickout -= self.STEP_PARAM / lvl
        elif direction == "out":
            self.stickout += self.STEP_PARAM / lvl
        elif direction == "cw":
            self.gfudge += self.STEP_GFUDGE / lvl
        elif direction == "ccw":
            self.gfudge -= self.STEP_GFUDGE / lvl
        self.stickout = max(-8.0, min(8.0, self.stickout))
        self.gfudge = max(-30.0, min(30.0, self.gfudge))
        self._refresh()
        self._move()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _save(self):
        settings.set("arm.stickout", round(self.stickout, 3))
        settings.set("arm.gfudge", round(self.gfudge, 2))
        self.app.toast("Saved stickout + gfudge")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


class RadscaleScreen(kit.Screen):
    """Reach scale (radscale) calibration: a pawn sits on d1 (far); the player
    adjusts radscale (\u25B2\u25BC, radial) and gfudge (\u25C0\u25B6, lateral)
    until the gripper sits over the piece. Park on Save/Back."""

    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)
    STEP_PARAM = 0.01    # radscale per ▲▼ tap at precision 1
    STEP_GFUDGE = 1.0    # deg per ◀▶ tap at precision 1

    def __init__(self, app):
        super().__init__(app=app, title="Reach scale (radscale)")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self._prec = 0
        self.radscale = settings.get("arm.radscale", 1.0)
        self.gfudge = settings.get("arm.gfudge", 0.0)
        self.x = 0.5 * RD.squaresize
        self.y = 7.0 * RD.squaresize        # d1
        self.radial = math.sqrt(self.x * self.x + (self.y + RD.axistorow0) ** 2)
        self.ang_off = 90.0 + math.degrees(
            math.atan(self.x / (self.y + RD.axistorow0)))
        self.hover = max(2.0, RD.cruise_for_reach(self._reach()) - 2.0)
        self.cur = [self._reach(), self.hover, self._angle()]

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=192)
        cols.set_board(self.board, redraw=self._draw_board)
        self._draw_board()
        row = tk.Frame(cols.right, bg=kit.BG)
        row.pack(fill="x")
        pad = tk.Frame(row, bg=kit.BG)
        pad.pack(side="left")
        self._build_jog(pad)
        side = tk.Frame(row, bg=kit.BG, width=190)
        side.pack(side="left", fill="y", padx=8)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 14), justify="left")
        self.val_lbl.pack(anchor="w", pady=(0, 6))
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.GREEN,
                                       height=2, width=10)
        self.save_btn.pack(pady=3)

        self.instr_lbl = kit.FitLabel(cols.right, font=("Atkinson Hyperlegible", 16))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._refresh()
        self._go(wake=True)

    def _reach(self):
        return self.radscale * (self.radial + RD.stickout)

    def _angle(self):
        return self.gfudge + self.ang_off

    def _draw_board(self):
        try:
            board = [["."] * 8 for _ in range(8)]
            col = RD.xtrans["d"]
            r = 8 - 1
            board[r][col] = "P"
            self.board.draw(board, last_move=(r, col, r, col))
        except Exception:
            pass

    def _build_jog(self, pad):
        g = tk.Frame(pad, bg=kit.BG)
        g.pack()
        self.up_btn = kit.big_button(g, "\u25B2", lambda: self._jog("in"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.up_btn.grid(row=0, column=1)
        self.left_btn = kit.big_button(g, "\u25C0", lambda: self._jog("cw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=4,
                                       height=2, font=kit.F_BTN)
        self.left_btn.grid(row=1, column=0)
        self.dn_btn = kit.big_button(g, "\u25BC", lambda: self._jog("out"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.dn_btn.grid(row=2, column=1)
        self.rt_btn = kit.big_button(g, "\u25B6", lambda: self._jog("ccw"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN)
        self.rt_btn.grid(row=1, column=2)
        zbar = tk.Frame(pad, bg=kit.BG)
        zbar.pack(pady=(6, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec >= len(self.PREC_LEVELS) - 1:
            return
        self._prec += 1
        self._set_prec()

    def _prec_out(self):
        if self._prec <= 0:
            return
        self._prec -= 1
        self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled", bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    def _refresh(self):
        self.val_lbl.configure(text="radscale %.3f\ngfudge %.2f\u00b0"
                               % (self.radscale, self.gfudge))
        self.instr_lbl.configure(
            text="Pawn on d1: \u25B2\u25BC = reach, \u25C0\u25B6 = swing. "
                 "Centre the gripper over the piece, then Save.")

    def _go(self, wake=False):
        reach, Y, Z = self._reach(), self.hover, self._angle()
        self.cur = [reach, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                if wake:
                    RD.energise()
                RD.movearm(reach, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _move(self):
        if self.busy:
            return
        self.busy = True
        reach, Y, Z = self._reach(), self.hover, self._angle()
        self.cur = [reach, Y, Z]

        def run():
            try:
                RD.targetXYZGWAWR = (reach, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors()
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        if direction == "in":
            self.radscale -= self.STEP_PARAM / lvl
        elif direction == "out":
            self.radscale += self.STEP_PARAM / lvl
        elif direction == "cw":
            self.gfudge += self.STEP_GFUDGE / lvl
        elif direction == "ccw":
            self.gfudge -= self.STEP_GFUDGE / lvl
        self.radscale = max(0.3, min(2.0, self.radscale))
        self.gfudge = max(-30.0, min(30.0, self.gfudge))
        self._refresh()
        self._move()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _save(self):
        settings.set("arm.radscale", round(self.radscale, 4))
        settings.set("arm.gfudge", round(self.gfudge, 2))
        self.app.toast("Saved radscale + gfudge")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


class CaptureDropScreen(kit.Screen):
    """Where the arm drops CAPTURED pieces (2026-11-18): a box hanging off
    the bottom of the board replaces the graveyard grid. The player jogs the
    arm above the box with the board calibration's reach / turn / height
    controls and taps OK to store the spot; after a capture the arm picks the
    piece up, moves here and opens the gripper (LEGO_Chess_rpd.takepiece)."""

    X_LO, X_HI = 4.5, 14.66
    Z_LO, Z_HI = 20.0, 160.0
    Y_LO, Y_HI = 1.6, 7.5
    STEP_REACH = 1.8
    STEP_BASE = 9.0
    STEP_Y = 0.5
    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)

    def __init__(self, app):
        super().__init__(app=app, title="Capture drop")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self._prec = 2            # default precision 3, as on Board calibration
        saved = settings.get("arm.capture_drop")
        X = Y = Z = None
        if isinstance(saved, dict):
            try:
                X, Y, Z = (float(saved["reach"]), float(saved["height"]),
                           float(saved["turn"]))
            except (KeyError, TypeError, ValueError):
                X = Y = Z = None
        if X is None:
            # no saved spot yet: start over the middle of the far edge (the
            # box hangs off the bottom of the board) at grip height
            X, Z = RD.armlength("d1"), RD.armangle("d1")
            Y = RD.grip_y("p", X)
        self.cur = [self._cx(X, Y), self._cy(Y), self._cz(Z)]

        # same layout as the board calibration: the jog on the left, the
        # readout and actions on the right, the instruction box full width
        # at the bottom
        main = tk.Frame(self.body, bg=kit.BG)
        main.pack(fill="both", expand=True)
        pad = tk.Frame(main, bg=kit.BG)
        pad.pack(side="left", anchor="n")
        self._build_jog(pad)
        side = tk.Frame(main, bg=kit.BG)
        side.pack(side="right", fill="y", expand=True)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 11), justify="center",
                                wraplength=170)
        self.val_lbl.pack(pady=(0, 6))
        kit.big_button(side, "OK", self._ok, bg=kit.GREEN, fg="#1b1e24",
                       height=2, width=12).pack(pady=4)

        self.instr_lbl = tk.Label(
            self.body, bg=kit.BG, fg=kit.FG,
            font=("Atkinson Hyperlegible", 13), wraplength=560,
            justify="center",
            text="position the arm directly above the box where captured "
                 "pieces should be stored")
        self.instr_lbl.pack(fill="x", pady=(8, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._refresh()
        self._poll()
        self._enter()

    # ---- clamps: the board calibration's working envelope ----
    def _cx(self, X, Y):
        cap = RD.reach_at_height(Y) - 0.1
        return max(self.X_LO, min(min(self.X_HI, cap), X))

    def _cy(self, Y):
        return max(self.Y_LO, min(self.Y_HI, Y))

    def _cz(self, Z):
        return max(self.Z_LO, min(self.Z_HI, Z))

    def _build_jog(self, pad):
        # the board calibration's controls: reach / turn arrows, the height
        # arrows and the precision divider
        g = tk.Frame(pad, bg=kit.BG)
        g.pack()
        kw = dict(bg=kit.CARD_HI, fg=kit.FG, width=8, height=2, font=kit.F_BTN)
        kit.big_button(g, "\u25B2", lambda: self._jog("in"), **kw).grid(
            row=0, column=1, padx=4, pady=1)
        kit.big_button(g, "\u25C0", lambda: self._jog("cw"), **kw).grid(
            row=1, column=0, padx=4, pady=1)
        kit.big_button(g, "\u25BC", lambda: self._jog("out"), **kw).grid(
            row=2, column=1, padx=4, pady=1)
        kit.big_button(g, "\u25B6", lambda: self._jog("ccw"), **kw).grid(
            row=1, column=2, padx=4, pady=1)
        bottom = tk.Frame(pad, bg=kit.BG)
        bottom.pack(pady=(8, 0))
        h = tk.Frame(bottom, bg=kit.BG)
        h.pack(side="left")
        kit.big_button(h, "\u25B2", lambda: self._h_jog(1), bg=kit.CARD_HI,
                       fg=kit.FG, width=5, height=2, font=kit.F_BTN).pack(
            side="left", padx=4)
        tk.Label(h, text="height", bg=kit.BG, fg=kit.FG_DIM,
                 font=kit.F_SMALL).pack(side="left", padx=(0, 4))
        kit.big_button(h, "\u25BC", lambda: self._h_jog(-1), bg=kit.CARD_HI,
                       fg=kit.FG, width=5, height=2, font=kit.F_BTN).pack(
            side="left", padx=4)
        zbar = tk.Frame(bottom, bg=kit.BG)
        zbar.pack(side="left", padx=(12, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec < len(self.PREC_LEVELS) - 1:
            self._prec += 1
            self._set_prec()

    def _prec_out(self):
        if self._prec > 0:
            self._prec -= 1
            self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled",
                bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        X, Y, Z = self.cur
        if direction == "in":
            X -= self.STEP_REACH / lvl
        elif direction == "out":
            X += self.STEP_REACH / lvl
        elif direction == "cw":
            Z += self.STEP_BASE / lvl
        elif direction == "ccw":
            Z -= self.STEP_BASE / lvl
        self.cur = [self._cx(X, Y), Y, self._cz(Z)]
        self._refresh()
        self._move()

    def _h_jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        X, Y, Z = self.cur
        Y = self._cy(Y + direction * self.STEP_Y / lvl)
        self.cur = [self._cx(X, Y), Y, self._cz(Z)]
        self._refresh()
        self._move()

    def _refresh(self):
        X, Y, Z = self.cur
        self.val_lbl.configure(
            text="reach %.2f\nturn %.1f\nheight %.2f" % (X, Z, Y))

    def _enter(self):
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                RD.energise()
                X, Y, Z = self.cur
                RD.movearm(X, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _move(self):
        if self.busy:
            return
        self.busy = True
        X, Y, Z = self.cur

        def run():
            try:
                RD.targetXYZGWAWR = (X, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors(pick_lean=True)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _ok(self):
        """Store the spot (2026-11-18): when the player has moved the arm to
        the right point, they tap OK to store that place."""
        X, Y, Z = self.cur
        settings.set("arm.capture_drop", {"reach": round(X, 2),
                                          "turn": round(Z, 1),
                                          "height": round(Y, 2)})
        self.app.toast("Capture drop saved")
        self._leave()

    def _leave(self):
        """Park and pop back - shared by OK and the back arrow."""
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _cleanup(self):
        self.stop = True

    def request_back(self):
        """Back arrow leaves WITHOUT saving - only OK stores the spot."""
        self._leave()


class CornerCalibScreen(kit.Screen):
    """Mode B 'arm frame' calibration: the player centres a pawn on each grid
    square and jogs the open jaws onto it AT GRIP HEIGHT (the height a real pick
    grips a pawn at), so the arm's real (reach, angle) is measured exactly where
    the grip happens. The measurement set is the 3x3 lattice - corners, edge
    midpoints and the centre (2026-10-22): corners alone left interior squares
    like d4 riding the CHORD through a curved (flexing) arm response - dead on
    at the corners, too far at the middle. fit_corner_frame() solves the pivot +
    scale + offset + angle offset (absorbing board position/orientation/size)
    and the per-square corrections then reproduce every measured square
    exactly. The fit is planar (X/Z only) - Y just sets the height a sample is
    taken at. Sweep: A8 D8 H8 -> H4 D4 A4 -> A1 D1 H1."""

    ORDER = ("a8", "d8", "h8", "h4", "d4", "a4", "a1", "d1", "h1")
    LABELS = dict((sq, sq.upper()) for sq in ORDER)
    X_LO, X_HI = 4.5, 14.66
    Z_LO, Z_HI = 20.0, 160.0
    Y_LO, Y_HI = 1.6, 7.5
    BAR_W, BAR_H = 56, 170
    PAD = 14
    STEP_REACH = 1.8
    STEP_BASE = 9.0
    STEP_Y = 0.5        # height inches per tap at precision 1 (2026-11-07)
    PREC_LEVELS = (1.0, 5.0, 10.0, 20.0)

    def __init__(self, app):
        super().__init__(app=app, title="Arm frame (corners)")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.samples = []
        self.fit = None
        self.frame_out = None
        self.corr_out = []
        self.sidx = 0
        self._prec = 2            # default precision 3 (2026-11-07)
        # Measure AT the pick (grip) height - _grip_Y() is the wrist height a
        # game pick grips a pawn at for this reach - NOT at a hover above it:
        # the centring is judged where the jaws actually grip, and perspective
        # shifts the perceived centre between hover and grip height. The height
        # is not a fit input (the frame fit is planar, X/Z only), only the
        # height the sample is taken at. auto_h keeps the tip at grip height as
        # the reach jogs; a HEIGHT-bar override wins until the next corner.
        self.auto_h = True
        X0, Z0 = RD.armlength(self.ORDER[0]), RD.armangle(self.ORDER[0])
        self.cur = [X0, self._grip_Y(X0), Z0]

        # Layout (2026-11-07): no board drawing - the buttons take the
        # space.  Left: the jog (big arrows + height arrows + precision);
        # right: the readout and the action buttons, centred.  The
        # instruction box is full width at the BOTTOM of the screen.
        main = tk.Frame(self.body, bg=kit.BG)
        main.pack(fill="both", expand=True)
        pad = tk.Frame(main, bg=kit.BG)
        pad.pack(side="left", anchor="n")
        self._build_jog(pad)
        side = tk.Frame(main, bg=kit.BG)
        side.pack(side="right", fill="y", expand=True)
        self.val_lbl = tk.Label(side, text="", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 11), justify="center",
                                wraplength=170)
        self.val_lbl.pack(pady=(0, 6))
        self.grab_btn = kit.big_button(side, "Grab", self._grab, bg=kit.ACCENT,
                                       height=2, width=12)
        self.grab_btn.pack(pady=4)
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.GREEN,
                                       height=2, width=12, disabled=True)
        self.save_btn.pack(pady=4)

        # the instruction box: full horizontal width at the bottom, with the
        # square name in bold
        self.instr_lbl = tk.Text(self.body, height=2, width=52, bg=kit.BG,
                                 fg=kit.FG, relief="flat", wrap="word",
                                 font=("Atkinson Hyperlegible", 13), padx=0, pady=0,
                                 highlightthickness=0, cursor="arrow",
                                 state="disabled")
        self.instr_lbl.tag_config("b", font=("Atkinson Hyperlegible", 13, "bold"))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.instr_lbl.bind("<Configure>", lambda *_: self._fit_instr())
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._enter()

    def _say(self, text, bold=None, color=None):
        """Instruction text with an optional bold run (the square name)."""
        try:
            w = self.instr_lbl
            w.configure(state="normal")
            w.delete("1.0", "end")
            if bold and bold in text:
                a, b = text.split(bold, 1)
                if a:
                    w.insert("end", a)
                w.insert("end", bold, ("b",))
                if b:
                    w.insert("end", b)
            else:
                w.insert("end", text)
            w.configure(state="disabled", foreground=color or kit.FG)
        except tk.TclError:
            return
        self._fit_instr()

    def _fit_instr(self):
        """Size the instruction box to its wrapped height (2026-11-07) -
        CAPPED at 4 lines: at the un-realized width the wrapped count is
        huge and the request used to blow the body out of the window."""
        try:
            w = self.instr_lbl
            n = w.count("1.0", "end-1c", "displaylines")
            n = n[0] if isinstance(n, tuple) else n
            n = max(2, min(3, int(n or 1)))
            if int(w.cget("height")) != n:
                w.configure(height=n)
        except tk.TclError:
            pass

    def _build_jog(self, pad):
        # BIG jog arrows (2026-11-07): the old ones were too small to
        # manipulate - wide touch targets now that the board is gone.
        g = tk.Frame(pad, bg=kit.BG)
        g.pack()
        self.up_btn = kit.big_button(g, "\u25B2", lambda: self._jog("in"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=8,
                                     height=2, font=kit.F_BTN)
        self.up_btn.grid(row=0, column=1, padx=4, pady=1)
        self.left_btn = kit.big_button(g, "\u25C0", lambda: self._jog("cw"),
                                       bg=kit.CARD_HI, fg=kit.FG, width=8,
                                       height=2, font=kit.F_BTN)
        self.left_btn.grid(row=1, column=0, padx=4, pady=1)
        self.dn_btn = kit.big_button(g, "\u25BC", lambda: self._jog("out"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=8,
                                     height=2, font=kit.F_BTN)
        self.dn_btn.grid(row=2, column=1, padx=4, pady=1)
        self.rt_btn = kit.big_button(g, "\u25B6", lambda: self._jog("ccw"),
                                     bg=kit.CARD_HI, fg=kit.FG, width=8,
                                     height=2, font=kit.F_BTN)
        self.rt_btn.grid(row=1, column=2, padx=4, pady=1)
        # height arrows (the touch bar retired) + precision share one row
        bottom = tk.Frame(pad, bg=kit.BG)
        bottom.pack(pady=(8, 0))
        h = tk.Frame(bottom, bg=kit.BG)
        h.pack(side="left")
        self.h_up_btn = kit.big_button(h, "\u25B2", lambda: self._h_jog(1),
                                       bg=kit.CARD_HI, fg=kit.FG, width=5,
                                       height=2, font=kit.F_BTN)
        self.h_up_btn.pack(side="left", padx=4)
        tk.Label(h, text="height", bg=kit.BG, fg=kit.FG_DIM,
                 font=kit.F_SMALL).pack(side="left", padx=(0, 4))
        self.h_dn_btn = kit.big_button(h, "\u25BC", lambda: self._h_jog(-1),
                                       bg=kit.CARD_HI, fg=kit.FG, width=5,
                                       height=2, font=kit.F_BTN)
        self.h_dn_btn.pack(side="left", padx=4)
        zbar = tk.Frame(bottom, bg=kit.BG)
        zbar.pack(side="left", padx=(12, 0))
        self.prec_out_btn = kit.big_button(zbar, "\u2212", self._prec_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=5, height=2, font=kit.F_BTN,
                                           disabled=True)
        self.prec_out_btn.pack(side="left", padx=(0, 4))
        self.prec_lbl = tk.Label(zbar, text="Precision 1", bg=kit.BG,
                                 fg=kit.FG_DIM, font=kit.F_SMALL)
        self.prec_lbl.pack(side="left", padx=(0, 4))
        self.prec_in_btn = kit.big_button(zbar, "+", self._prec_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=5, height=2, font=kit.F_BTN)
        self.prec_in_btn.pack(side="left")
        self._set_prec()

    def _prec_in(self):
        if self._prec >= len(self.PREC_LEVELS) - 1:
            return
        self._prec += 1
        self._set_prec()

    def _prec_out(self):
        if self._prec <= 0:
            return
        self._prec -= 1
        self._set_prec()

    def _set_prec(self):
        try:
            self.prec_lbl.configure(text="Precision %d" % (self._prec + 1))
            self.prec_in_btn.configure(
                state="normal" if self._prec < len(self.PREC_LEVELS) - 1
                else "disabled", bg=kit.CARD_HI if self._prec < len(self.PREC_LEVELS) - 1
                else kit.CARD, fg=kit.FG)
            self.prec_out_btn.configure(
                state="normal" if self._prec > 0 else "disabled",
                bg=kit.CARD_HI if self._prec > 0 else kit.CARD,
                fg=kit.FG if self._prec > 0 else kit.FG_DIM)
        except tk.TclError:
            pass

    # ---- height arrows (the touch bar retired, 2026-11-07) ----
    def _h_jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        X, Y, Z = self.cur
        Y += direction * self.STEP_Y / lvl
        Y = max(self.Y_LO, min(self.Y_HI, Y))
        self.cur = [X, Y, Z]
        self.auto_h = False   # the player's height wins until the next corner
        self._refresh()
        self._move()

    def _grip_Y(self, X):
        """Wrist height whose gripper tip sits at a PAWN's grip height for this
        reach - exactly the height a game pick grips at (clamped to the bar)."""
        return max(self.Y_LO, min(self.Y_HI, RD.grip_y("p", X)))

    def _enter(self):
        self._refresh()
        self._go(self.ORDER[0], wake=True)

    def _go(self, sq, wake=False):
        # The square's coordinates come from the CURRENT board mapping -
        # whatever it is today (2026-11-07): the last good fit is a far
        # better seed than the retired parametric model, and if the mapping
        # IS the parametric one this is identical.  Clamped so the low
        # descend stays inside the reach annulus.
        X, Z = RD.armlength(sq), RD.armangle(sq)
        Y = self._grip_Y(X)
        cap = RD.reach_at_height(Y) - 0.1
        X = max(self.X_LO, min(cap, X))
        Y = self._grip_Y(X)      # grip height at the CLAMPED reach
        self.auto_h = True
        self.cur = [X, Y, Z]
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                if wake:
                    RD.energise()
                RD.movearm(X, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _refresh(self):
        sq = self.ORDER[self.sidx]
        lbl = self.LABELS[sq]
        self._say("Center a pawn on %s. Adjust the gripper to pick up the "
                  "pawn, and press Grab." % lbl, bold=lbl)
        self.val_lbl.configure(
            text="%s   reach %.2f   angle %.1f   %d/%d grabbed"
                 % (lbl, self.cur[0], self.cur[2],
                    len(self.samples), len(self.ORDER)))

    def _jog(self, direction):
        if self.busy:
            return
        lvl = self.PREC_LEVELS[self._prec]
        X, Y, Z = self.cur
        if direction == "in":
            X -= self.STEP_REACH / lvl
        elif direction == "out":
            X += self.STEP_REACH / lvl
        elif direction == "cw":
            Z += self.STEP_BASE / lvl
        elif direction == "ccw":
            Z -= self.STEP_BASE / lvl
        cap = RD.reach_at_height(Y) - 0.1
        X = max(self.X_LO, min(min(self.X_HI, cap), X))
        Z = max(self.Z_LO, min(self.Z_HI, Z))
        if self.auto_h and direction in ("in", "out"):
            Y = self._grip_Y(X)  # keep the tip at grip height at the new reach
        self.cur = [X, Y, Z]
        self._refresh()
        self._move()

    def _move(self):
        if self.busy:
            return
        self.busy = True
        X, Y, Z = self.cur

        def run():
            try:
                RD.targetXYZGWAWR = (X, Y, Z, RD.targetXYZGWAWR[3], -90,
                                     RD.targetXYZGWAWR[5])
                RD.movemotors(pick_lean=True)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _grab(self):
        """The pick TEST (2026-11-07): the jaws close on the pawn and
        release - 'adjust the gripper to pick up the pawn, and press Grab'.
        The instruction says 'testing' while it runs, then the results are
        displayed and the Save button is enabled."""
        if self.busy:
            return
        self._say("testing")
        self.busy = True
        jaw = RD.grip_width("p")

        def run():
            try:
                RD.set_gripper(jaw)          # close on the pawn
                time.sleep(1.2)              # hold the grip for the player
                RD.set_gripper(RD.grip_open_width())
            except Exception:
                pass
            self.q.put(("grabbed", self.cur[0], self.cur[2], jaw))
        threading.Thread(target=run, daemon=True).start()

    def _record_sample(self, X, Z, jaw):
        sq = self.ORDER[self.sidx]
        lbl = self.LABELS[sq]
        self.samples.append({"square": sq, "X": X, "Z": Z,
                             "Y": self.cur[1], "auto_h": bool(self.auto_h)})
        self.save_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")
        res = ("Test %s \u2014 jaws %.1f, reach %.2f in, angle %.1f\u00b0. "
               % (lbl, jaw, X, Z))
        if self.sidx + 1 < len(self.ORDER):
            self.sidx += 1
            self._go(self.ORDER[self.sidx])
            nxt = self.LABELS[self.ORDER[self.sidx]]
            self._say(res + "Center a pawn on %s. Adjust the gripper to "
                      "pick up the pawn, and press Grab." % nxt, bold=nxt)
            self.val_lbl.configure(
                text="%s   reach %.2f   angle %.1f   %d/%d grabbed"
                     % (nxt, self.cur[0], self.cur[2],
                        len(self.samples), len(self.ORDER)))
        else:
            self._show_fit()

    def _show_fit(self):
        f = ap.fit_corner_frame(self.samples)
        self._log_run(self.samples, f)
        if f.get("ok"):
            self.fit = f
            # Round the frame EXACTLY as Save stores it, then compute the
            # per-corner corrections from those rounded values: the model then
            # lands on the MEASURED corners (predicted == measured) whatever
            # the plain 5-parameter fit left behind.
            self.frame_out = {"px": round(f["px"], 3), "py": round(f["py"], 3),
                              "scale": round(f["scale"], 4),
                              "offset": round(f["offset"], 3),
                              "a0": round(f["a0"], 2), "sign": int(f["sign"])}
            self.corr_out = self._corr(self.frame_out)
            self.grab_btn.configure(state="disabled", bg=kit.CARD, fg=kit.FG_DIM)
            self.val_lbl.configure(text=(
                "px %.2f  py %.2f\nscale %.3f  off %.3f\na0 %.2f  s %+.0f\n"
                "resid %.3f in  ang %.1f deg"
                % (f["px"], f["py"], f["scale"], f["offset"], f["a0"],
                   f["sign"], f["residual_in"], f["ang_residual_deg"])))
            self._say("Corners recorded. Save switches the arm to mode B "
                      "(corners).\n" + self._roundtrip_text(),
                      color=kit.GREEN)
            self.save_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")
        else:
            self._say(self._fail_text(f), color=kit.RED)

    def _corr(self, frame):
        """Per-corner corrections (measured MINUS the frame's prediction) -
        what planar_to_arm adds back so the calibrated corners are exact."""
        out = []
        for s in self.samples:
            Xp, Zp = ap.frame_predict(frame, s["square"])
            dz = (s["Z"] - Zp + 180.0) % 360.0 - 180.0
            out.append([s["square"], round(s["X"] - Xp, 3), round(dz, 2)])
        return out

    def _roundtrip_text(self):
        """The automatic calibration check: what the RUNTIME will command at
        each measured square (frame + corrections) vs the pose measured there.
        PASS = predicted == measured. The corrections show how much the bare
        frame was bent at each square (the drift it would have left)."""
        corr = dict((c[0], (c[1], c[2])) for c in self.corr_out)
        names, dr, da, dt, bad = [], [], [], [], []
        for s in self.samples:
            Xp, Zp = ap.frame_predict(self.frame_out, s["square"])
            c_reach, c_ang = corr[s["square"]]
            Xp, Zp = Xp + c_reach, Zp + c_ang
            er = Xp - s["X"]
            ea = (Zp - s["Z"] + 180.0) % 360.0 - 180.0
            names.append(self.LABELS[s["square"]])
            dr.append(c_reach)
            da.append(c_ang)
            dt.append(RD.grip_y("p", Xp) - s["Y"])
            if not (abs(er) <= 0.02 and abs(ea) <= 0.05):
                bad.append(self.LABELS[s["square"]])
        w = max(range(len(dt)), key=lambda i: abs(dt[i]))
        star = "" if self.samples[w].get("auto_h", True) else "*"
        return "\n".join([
            "round-trip: " + ("all %d PASS" % len(names) if not bad
                              else "FAIL " + " ".join(bad)),
            "corr in/deg:  " + "  ".join("%s %+.2f/%+.1f" % (n, r, a)
                                         for n, r, a in zip(names, dr, da)),
            "tip in (*=hand): worst %+.2f at %s%s"
            % (dt[w], names[w], star)])

    def _fail_text(self, f):
        """Failure reason + WHICH corner disagrees (per-corner reach/angle
        residuals), so a bad measurement is named instead of hidden behind one
        RMS number."""
        names = [self.LABELS[s["square"]] for s in self.samples]
        rr = f.get("reach_res") or []
        ar = f.get("ang_res") or []
        lines = ["Fit failed: %s" % f.get("reason", "?")]
        if rr:
            lines.append("reach in: " + "  ".join(
                "%s %+.2f" % (n, v) for n, v in zip(names, rr)))
        if ar:
            lines.append("angle deg: " + "  ".join(
                "%s %+.1f" % (n, v) for n, v in zip(names, ar)))
        return "\n".join(lines)

    def _log_run(self, samples, fit):
        """Snapshot the run (raw samples + fit verdict) to data/ so a failed
        fit can be diagnosed offline instead of re-run blind."""
        try:
            path = os.path.join(os.path.dirname(settings.USER_PATH),
                                "corner_last.json")
            with open(path, "w") as fh:
                json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "samples": samples, "fit": fit}, fh, indent=1,
                          default=float)
        except Exception:
            pass

    def _match(self):
        """RETIRED (2026-11-07): the Match height button is gone - it fitted
        pickheightadj/pickheightreachslope, both retired from the grip model
        when the per-piece grip heights replaced them."""
        self.app.toast("Match height is retired - see Board & Pieces")

    def _apply_match(self, adj, slope):
        pass

    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "done":
                    self.busy = False
                elif ev[0] == "grabbed":
                    self.busy = False
                    self._record_sample(ev[1], ev[2], ev[3])
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _save(self):
        if self.frame_out is None:
            # Save is enabled after every test (2026-11-07): fit with what
            # has been measured so far before persisting
            self._show_fit()
            if self.frame_out is None:
                return
        fr = self.frame_out
        if fr is None:
            return
        settings.set("arm.frame_px", fr["px"])
        settings.set("arm.frame_py", fr["py"])
        settings.set("arm.frame_scale", fr["scale"])
        settings.set("arm.frame_offset", fr["offset"])
        settings.set("arm.frame_a0", fr["a0"])
        settings.set("arm.frame_sign", fr["sign"])
        settings.set("arm.corner_res", self.corr_out)
        settings.set("arm.calib_mode", "B")
        self.app.toast("Corner frame saved (mode B)")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


class ManualCalibScreen(kit.Screen):
    """Embedded four-corner click calibration: same layout as the other
    calibration screens. Tap the four named outer corners in order; a warped
    grid preview appears once all four are set; Save enabled only then.
    Used when automatic board detection cannot lock."""

    ORDER = (("A1", "BOTTOM-LEFT corner of the board"),
             ("H1", "BOTTOM-RIGHT corner of the board"),
             ("H8", "TOP-RIGHT corner of the board"),
             ("A8", "TOP-LEFT corner of the board"))

    def __init__(self, app):
        super().__init__(app=app, title="Manual corners")
        self.app = app
        self.snap = settings.snapshot()
        self._lock = threading.Lock()
        self._frame = None
        self._stop = False
        self.ZOOMS = (1, 3, 9)          # live-feed magnifications
        self.clicks = []          # committed corners: full-res px [(x,y)]
        self.aim = None           # pending tap for the current corner (full px)
        self.ready = False
        self._zi = 0              # index into ZOOMS
        self._ctr = None          # crop centre (normalised), current corner

        row = tk.Frame(self.body, bg=kit.BG)
        row.pack()
        zc = tk.Frame(row, bg=kit.BG)
        zc.pack(side="left", fill="y", padx=(0, 6))
        self.zoom_in_btn = kit.big_button(zc, "+", self._zoom_in,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=2, height=1, font=kit.F_BTN)
        self.zoom_in_btn.pack(pady=(0, 6))
        self.zoom_out_btn = kit.big_button(zc, "\u2212", self._zoom_out,
                                           bg=kit.CARD, fg=kit.FG_DIM,
                                           width=2, height=1, font=kit.F_BTN,
                                           disabled=True)
        self.zoom_out_btn.pack()

        self.view = CanvasView(row, w=480, h=270)
        self.view.canvas.pack(side="left")
        self.view.canvas.bind("<Button-1>", self._on_tap)

        side = tk.Frame(row, bg=kit.BG, width=132)
        side.pack(side="left", fill="y", padx=6)
        self.save_btn = kit.big_button(side, "Save", self._save, bg=kit.CARD,
                                       fg=kit.FG_DIM, height=2, width=11,
                                       disabled=True)
        self.save_btn.pack(pady=(4, 8))
        self.ok_btn = kit.big_button(side, "OK", self._ok, bg=kit.CARD,
                                     fg=kit.FG_DIM, height=2, width=11,
                                     disabled=True)
        self.ok_btn.pack(pady=8)
        kit.big_button(side, "Retry", self._retry, bg=kit.CARD,
                       fg=kit.FG, height=2, width=11).pack(pady=8)
        self.instr = kit.instr_label(self.body,
                                     font=("Atkinson Hyperlegible", 15, "bold"))
        self.instr.pack(fill="x", pady=(4, 0))
        self._say("Waiting for camera...", kit.FG_DIM)
        threading.Thread(target=self._cam, daemon=True).start()

        def _on_destroy(_e):
            self._stop = True   # _cam thread's finally releases the cap
        self.bind("<Destroy>", _on_destroy, add="+")
        self._pump()

    def request_back(self):
        self._cancel()

    def _say(self, text, color=None):
        try:
            self.instr.configure(text=text, fg=color or kit.FG)
        except tk.TclError:
            pass

    def _cam(self):
        camera.lock_camera()
        cap = camera.open_camera()
        self._cap = cap
        if cap is None:
            self._say("Camera busy - close the other camera program, "
                      "then go Back and retry.", kit.RED)
            return
        try:
            for _ in range(20):
                cap.read()
            while not self._stop:
                ok, f = cap.read()
                if not ok:
                    time.sleep(0.2)
                    continue
                with self._lock:
                    self._frame = f
        finally:
            camera.close_camera(cap)
            self._cap = None

    def _pump(self):
        if self._stop:
            return
        try:
            with self._lock:
                f = None if self._frame is None else self._frame.copy()
            if f is not None:
                if self.ready:
                    self.view.show(self._preview(f))
                    self._say("Check the grid lines up with the squares - "
                              "then Save.", kit.GREEN)
                else:
                    self._render(f)
                    self._update_status()
        except tk.TclError:
            return
        self.after(100, self._pump)

    # ---- corner model + live-feed zoom --------------------------------------

    def _known(self, name):
        v = _points_vals()
        if not v or len(v) < 8:
            return None
        i = {"A1": 0, "H1": 2, "A8": 4, "H8": 6}[name]
        return (v[i], v[i + 1])

    def _cur_corner(self):
        return self.ORDER[min(len(self.clicks), 3)][0]

    def _recalc_ctr(self):
        with self._lock:
            f = self._frame
        if self.aim is not None and f is not None:
            h, w = f.shape[:2]
            self._ctr = (self.aim[0] / w, self.aim[1] / h)
        else:
            self._ctr = self._known(self._cur_corner())

    def _window(self):
        z = self.ZOOMS[self._zi]
        if z <= 1 or self._ctr is None:
            return (0.0, 0.0, 1.0, 1.0)
        hw = 1.0 / (2.0 * z)
        x0 = max(0.0, min(1.0 - 2 * hw, self._ctr[0] - hw))
        y0 = max(0.0, min(1.0 - 2 * hw, self._ctr[1] - hw))
        return (x0, y0, 2 * hw, 2 * hw)

    def _render(self, f):
        h, w = f.shape[:2]
        x0, y0, ww, wh = self._window()
        crop = f[int(y0 * h):int((y0 + wh) * h),
                 int(x0 * w):int((x0 + ww) * w)]
        if crop.size == 0:
            return
        disp = cv2.resize(crop, (self.view.w, self.view.h))
        for (cx, cy), (lbl, _d) in zip(self.clicks, self.ORDER):
            nx = (cx / w - x0) / ww
            ny = (cy / h - y0) / wh
            if 0 <= nx <= 1 and 0 <= ny <= 1:
                X, Y = int(nx * self.view.w), int(ny * self.view.h)
                cv2.circle(disp, (X, Y), 8, (0, 255, 0), -1)
                cv2.putText(disp, lbl, (X + 11, Y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        self.view.show(disp)

    def _draw_aim(self, x, y):
        c = self.view.canvas
        c.delete("aim")
        r = 12
        c.create_oval(x - r, y - r, x + r, y + r, outline="#ff4444", width=3,
                      tags="aim")
        c.create_line(x - r - 6, y, x + r + 6, y, fill="#ff4444", width=2,
                      tags="aim")
        c.create_line(x, y - r - 6, x, y + r + 6, fill="#ff4444", width=2,
                      tags="aim")

    def _clear_aim(self):
        try:
            self.view.canvas.delete("aim")
        except tk.TclError:
            pass

    def _on_tap(self, evt):
        with self._lock:
            if self._frame is None:
                return
        if self.ready:
            self._retry()
            return
        x0, y0, ww, wh = self._window()
        nx = x0 + (evt.x / float(self.view.w)) * ww
        ny = y0 + (evt.y / float(self.view.h)) * wh
        with self._lock:
            h, w = self._frame.shape[:2]
        self.aim = (max(0, min(w - 1, int(nx * w))),
                    max(0, min(h - 1, int(ny * h))))
        self._draw_aim(evt.x, evt.y)
        self._set_ok_enabled(True)
        if self._zi == 0:
            self._recalc_ctr()          # prime the zoom centre from this tap

    def _ok(self):
        if self.aim is None:
            return
        self.clicks.append(self.aim)
        self.aim = None
        self._clear_aim()
        self._set_ok_enabled(False)
        if len(self.clicks) >= 4:
            self.ready = True
            self.save_btn.configure(state="normal", bg=kit.ACCENT,
                                    fg="#1b1e24")
            return
        self._recalc_ctr()
        if self._ctr is None and self._zi > 0:
            self._zi = 0                # new corner unknown -> back to 1x
        self._update_zoom_btns()

    def _set_ok_enabled(self, on):
        try:
            if on:
                self.ok_btn.configure(state="normal", bg=kit.ACCENT,
                                      fg="#1b1e24")
            else:
                self.ok_btn.configure(state="disabled", bg=kit.CARD,
                                      fg=kit.FG_DIM)
        except tk.TclError:
            pass

    def _update_zoom_btns(self):
        try:
            if self._zi >= len(self.ZOOMS) - 1:
                self.zoom_in_btn.configure(state="disabled", bg=kit.CARD,
                                           fg=kit.FG_DIM)
            else:
                self.zoom_in_btn.configure(state="normal", bg=kit.CARD_HI,
                                           fg=kit.FG)
            if self._zi <= 0:
                self.zoom_out_btn.configure(state="disabled", bg=kit.CARD,
                                            fg=kit.FG_DIM)
            else:
                self.zoom_out_btn.configure(state="normal", bg=kit.CARD_HI,
                                            fg=kit.FG)
        except tk.TclError:
            pass

    def _zoom_in(self):
        if self._zi >= len(self.ZOOMS) - 1:
            return
        if self._ctr is None:
            self._recalc_ctr()
        if self._ctr is None:
            return                       # nothing to centre on yet
        self._zi += 1
        self._update_zoom_btns()

    def _zoom_out(self):
        if self._zi <= 0:
            return
        self._zi -= 1
        self._update_zoom_btns()

    def _retry(self):
        self.clicks = []
        self.aim = None
        self.ready = False
        self._zi = 0
        self._recalc_ctr()
        self._update_zoom_btns()
        self._clear_aim()
        self._set_ok_enabled(False)
        self.save_btn.configure(state="disabled", bg=kit.CARD, fg=kit.FG_DIM)

    def _update_status(self):
        if self.ready:
            return
        _lbl, desc = self.ORDER[len(self.clicks)]
        self._say("Tap the %s (corner %d of 4), zoom for accuracy, then OK.%s"
                  % (desc, len(self.clicks) + 1,
                     "" if self._ctr else "  (tap once to enable zoom)"),
                  kit.ACCENT)
        self._set_ok_enabled(self.aim is not None)

    def _preview(self, f):
        h, w = f.shape[:2]
        d = 560
        src = np.float32([[x, y] for (x, y) in self.clicks])
        dst = np.float32([[0, d], [d, d], [d, 0], [0, 0]])  # A1,H1,H8,A8
        H = cv2.getPerspectiveTransform(src, dst)
        wp = cv2.warpPerspective(f, H, (d, d))
        step = d // 8
        for i in range(9):
            cv2.line(wp, (i * step, 0), (i * step, d), (0, 200, 255), 1)
            cv2.line(wp, (0, i * step), (d, i * step), (0, 200, 255), 1)
        for i, ch in enumerate("abcdefgh"):
            cv2.putText(wp, ch, (int((i + 0.25) * step), 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        for r in range(8):
            cv2.putText(wp, str(8 - r), (4, int((r + 0.7) * step)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        return wp

    def _save(self):
        if not self.ready:
            return
        with self._lock:
            f = self._frame
        h, w = f.shape[:2]
        a1, h1, h8, a8 = self.clicks        # ORDER is bl,br,tr,tl
        flat = []
        for (x, y) in (a1, h1, a8, h8):     # points.txt slots: A1,H1,A8,H8
            flat += ["%.6f" % (x / w), "%.6f" % (y / h)]
        with open(CBstate.mydir + "points.txt", "w") as fp:
            fp.write(",".join(flat))

        def after():
            settings.set("board.stale", 0)
            self.app.toast("Camera corners saved")
            self.app.back()
        after()

    def _cancel(self):
        if not self.begin_close():
            return
        self._stop = True
        settings.restore(self.snap)
        self.app.toast("Manual corners cancelled - previous state restored")
        self.app.back()


# ── pick height settings ─────────────────────────────────────────────────────

class PickHeightSettingsScreen(kit.Screen):
    """Manual pick-height calibration: one screen for pickheightadj AND
    pickheightreachslope. A single file (1-8, 8 at top) is shown; tapping a
    square puts a pawn there in the UI and moves the arm to that square's
    projected location at grip height (jaws open). Sliders tune the two
    pick-height constants live; Save stores both."""

    FILE = "d"
    F_W, CELL = 84, 38
    STEP = {"adj": 0.05, "slope": 0.005}
    LO_HI = {"adj": (-3.0, 3.0), "slope": (-0.3, 0.3)}

    def __init__(self, app):
        super().__init__(app=app, title="Pick height settings")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.sel = None
        self._applied = None
        self.adj = float(settings.get("arm.pickheightadj", 0.0))
        self.slope = float(settings.get("arm.pickheightreachslope", 0.0))
        self.start_adj = self.adj
        self.start_slope = self.slope
        self.cur = [8.0, self._grip_y(8.0), 90.0]
        self._tuners = {}

        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both", expand=True)
        self.file_c = tk.Canvas(row, width=self.F_W, height=8 * self.CELL,
                                bg="#181b21", highlightthickness=0)
        self.file_c.pack(side="left", padx=(0, 12))
        self.file_c.bind("<Button-1>", self._on_file_tap)
        self._draw_file()

        right = tk.Frame(row, bg=kit.BG)
        right.pack(side="left", fill="both", expand=True, padx=8)
        ctl = tk.Frame(right, bg=kit.BG)
        ctl.pack(fill="x")
        self._build_tuner(ctl, "pickheightadj", "adj")
        self._build_tuner(ctl, "pickheightreachslope", "slope")
        btns = tk.Frame(ctl, bg=kit.BG)
        btns.pack(fill="x", pady=(10, 0))
        kit.big_button(btns, "Save", self._save, bg=kit.GREEN,
                       height=2, width=10).pack(side="left", padx=(0, 6))
        kit.big_button(btns, "Heights",
                       lambda: app.show(PieceHeightScreen),
                       bg=kit.CARD, fg=kit.FG, height=2, width=12).pack(
            side="left")

        self.instr_lbl = kit.FitLabel(right, font=("Atkinson Hyperlegible", 14))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self._set_instr()
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()

    # ---- model ----
    def _grip_y(self, reach):
        """Wrist height so the gripper tip lands at the grip point:
        Grip_height = piece_h + pickheightadj + pickheightreachslope * reach."""
        return (RD.pieceheights["p"] + self.adj + self.slope * reach
                + RD.gripper_len)

    def _set_instr(self):
        self.instr_lbl.configure(
            text="Select a square. Once the arm is in position, place a pawn "
                 "directly below the gripper and adjust sliders until the "
                 "gripper can grab the piece.")

    # ---- single-file strip ----
    def _draw_file(self):
        c = self.file_c
        c.delete("all")
        for i in range(8):
            rank = 8 - i                      # top cell = rank 8
            y0 = i * self.CELL
            fill = "#2a3140" if self.sel == rank else "#20242c"
            c.create_rectangle(1, y0 + 1, self.F_W - 1, y0 + self.CELL - 1,
                               fill=fill, outline="#5a4c37")
            c.create_text(12, y0 + self.CELL // 2, text=str(rank),
                          font=("Atkinson Hyperlegible", 11, "bold"), fill=kit.FG_DIM)
            if self.sel == rank:
                cx, cy = self.F_W // 2, y0 + self.CELL // 2
                c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9,
                              fill="#f6f2e8", outline="#16181d")

    def _on_file_tap(self, evt):
        idx = evt.y // self.CELL
        if 0 <= idx < 8:
            self.sel = 8 - idx               # top cell = rank 8
            self._draw_file()
            self._go(self.FILE + str(self.sel))

    # ---- tuners ----
    def _build_tuner(self, parent, label, key):
        box = tk.Frame(parent, bg=kit.BG)
        box.pack(fill="x", pady=6)
        top = tk.Frame(box, bg=kit.BG)
        top.pack(fill="x")
        tk.Label(top, text=label, font=("Atkinson Hyperlegible", 12), bg=kit.BG,
                 fg=kit.FG).pack(side="left")
        val_lbl = tk.Label(top, text="", font=("JetBrains Mono", 11),
                           bg=kit.BG, fg=kit.ACCENT, justify="right")
        val_lbl.pack(side="right")
        sr = tk.Frame(box, bg=kit.BG)
        sr.pack(fill="x", pady=(2, 0))
        lo, hi = self.LO_HI[key]
        kit.big_button(sr, "\u2212", lambda: self._step(key, -1),
                       bg=kit.CARD, fg=kit.FG, width=3, height=1,
                       font=kit.F_BTN).pack(side="left")
        scale = tk.Scale(sr, from_=lo, to=hi, resolution=self.STEP[key],
                         orient="horizontal", bg=kit.BG, fg=kit.FG,
                         highlightthickness=0, troughcolor="#20242c",
                         command=lambda v, k=key: self._on_scale(k, float(v)))
        scale.pack(side="left", fill="x", expand=True, padx=4)
        kit.big_button(sr, "+", lambda: self._step(key, +1),
                       bg=kit.CARD_HI, fg=kit.FG, width=3, height=1,
                       font=kit.F_BTN).pack(side="left")
        self._tuners[key] = (val_lbl, scale, lo, hi)
        scale.set(self.adj if key == "adj" else self.slope)
        self._refresh_val(key)

    def _refresh_val(self, key):
        lbl = self._tuners[key][0]
        if key == "adj":
            lbl.configure(text="cur %.2f   start %.2f"
                          % (self.adj, self.start_adj))
        else:
            lbl.configure(text="cur %.3f   start %.3f"
                          % (self.slope, self.start_slope))

    def _on_scale(self, key, v):
        if key == "adj":
            self.adj = v
        else:
            self.slope = v
        self._refresh_val(key)
        self._live()

    def _step(self, key, sign):
        lo, hi = self.LO_HI[key]
        d = sign * self.STEP[key]
        v = (self.adj if key == "adj" else self.slope) + d
        v = max(lo, min(hi, v))
        self._tuners[key][1].set(v)     # move the slider (no command fired)
        self._on_scale(key, v)          # update value + live move

    # ---- arm ----
    def _go(self, sq):
        X, Z = RD.armlength(sq), RD.armangle(sq)
        Yw = self._grip_y(X)
        self.cur = [X, Yw, Z]
        if self.busy:
            return
        self.busy = True
        self._applied = Yw

        def run():
            try:
                RD.energise()
                RD.movearm(X, Z)
                RD.openjaws()
                RD.moveXY(Yw)          # descend with jaws open
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _live(self):
        if self.sel is None:
            return
        self.cur[1] = self._grip_y(self.cur[0])
        self._apply_height()

    def _apply_height(self):
        if self.busy:
            return
        if self._applied is not None and abs(self.cur[1] - self._applied) < 1e-6:
            return
        self.busy = True
        self._applied = self.cur[1]
        Yw = self.cur[1]

        def run():
            try:
                RD.moveXY(Yw)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
                    self._apply_height()   # apply any pending slider change
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    # ---- save / leave ----
    def _save(self):
        kit.confirm(self.app, "Save",
                    "Store the pick height settings?", self._do_save,
                    yes_text="Save")

    def _do_save(self):
        settings.set("arm.pickheightadj", round(self.adj, 3))
        settings.set("arm.pickheightreachslope", round(self.slope, 4))
        self.app.toast("Saved pick height settings")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


# ── piece heights ─────────────────────────────────────────────────────────────

class PieceHeightScreen(kit.Screen):
    """Board & Pieces (2026-11-06): the GRIP HEIGHT of each piece and the
    board height (LEFT column), and a per-piece jaw adjust (RIGHT column),
    each piece on one row.  The grip height is the grab point's height above
    the piece's own sitting surface - the value you jog to and read; the
    model adds the board height and the arm sag compensation.  Rows follow
    the Calibration-settings template: name (unit), info button where it
    helps, tappable value -> the standard slider.  No info buttons on the
    piece heights (self-explanatory); the board height and each piece's jaw
    adjust carry one."""

    ROWS = (
        # key,   name,           lo,   hi,   step, height info (or None)
        ("board", "Board (in)", 0.0, 2.0, 0.1,
         "Height of the surface of the board off the table. This value "
         "sets the height adjust when moving pieces to the graveyard."),
        ("p", "Pawn (in)", 0.2, 3.5, 0.05, None),
        ("r", "Rook (in)", 0.2, 3.5, 0.05, None),
        ("n", "Knight (in)", 0.2, 3.5, 0.05, None),
        ("b", "Bishop (in)", 0.2, 3.5, 0.05, None),
        ("q", "Queen (in)", 0.2, 3.5, 0.05, None),
        ("k", "King (in)", 0.2, 3.5, 0.05, None),
    )
    NAMES = {"p": "Pawn", "r": "Rook", "n": "Knight", "b": "Bishop",
             "q": "Queen", "k": "King"}
    JAW_LO, JAW_HI, JAW_STEP = -20.0, 60.0, 1.0

    def __init__(self, app):
        super().__init__(app=app, title="Board & Pieces")
        self.app = app
        self.rows = {}
        self.jaws = {}
        grid = tk.Frame(self.body, bg=kit.BG)
        grid.pack(fill="both", expand=True)
        # name | grip height (left) | jaw adjust (right)
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        grid.columnconfigure(2, weight=3)
        for r in range(len(self.ROWS)):
            grid.rowconfigure(r, weight=1)
        for r, (key, label, lo, hi, step, definition) in enumerate(self.ROWS):
            name_cell = tk.Frame(grid, bg=kit.BG)
            name_cell.grid(row=r, column=0, sticky="nsew", pady=1)
            tk.Label(name_cell, text=label, font=("Atkinson Hyperlegible", 11),
                     bg=kit.BG, fg=kit.FG, anchor="w").pack(side="left")
            if definition:          # the board's info button
                kit.info_button(
                    name_cell,
                    lambda t=label, d=definition: self._info(t, d),
                    small=True, bg=kit.BG).pack(side="left", padx=(4, 0))
            hbtn = kit.big_button(
                grid, "",
                lambda k=key, l=label, lo=lo, hi=hi, st=step:
                self._edit_height(k, l, lo, hi, st),
                bg=kit.CARD_HI, fg=kit.ACCENT, width=4, height=1,
                font=("Atkinson Hyperlegible", 12))
            hbtn.grid(row=r, column=1, sticky="nsew", padx=(4, 0), pady=1)
            self.rows[key] = hbtn
            if key == "board":      # the board has no jaws
                continue
            cell = tk.Frame(grid, bg=kit.BG)
            cell.grid(row=r, column=2, sticky="nsew", padx=(4, 0), pady=1)
            jbtn = kit.big_button(
                cell, "", lambda k=key: self._edit_jaw(k),
                bg=kit.CARD_HI, fg=kit.ACCENT, width=4, height=1,
                font=("Atkinson Hyperlegible", 12))
            jbtn.pack(side="left", fill="both", expand=True)
            kit.info_button(cell, self._jaw_info_cb(key), small=True,
                            bg=kit.BG).pack(side="left", padx=(4, 0))
            self.jaws[key] = jbtn
        self.refresh_labels()

    def _jaw_info_cb(self, key):
        name = self.NAMES.get(key, key).lower()
        text = ("Controls the jaw width when gripping the %s (servo "
                "degrees). Lower values grip the %s tighter, higher values "
                "looser. Increase if the jaws fail to grip the %s; decrease "
                "if they pinch the sides or knock it over."
                % (name, name, name))
        return lambda t=self.NAMES.get(key, key) + " jaws", d=text: \
            self._info(t, d)

    def refresh_labels(self):
        ph = settings.get("arm.piece_grips") or RD.piecegrips
        pj = settings.get("arm.piece_jaws") or RD.piecejaws
        bh = float(settings.get("arm.board_height", 0.5))
        for key, btn in self.rows.items():
            if key == "board":
                btn.configure(text=_fmt(bh))
            else:
                btn.configure(text=_fmt(float(ph.get(key, 0))))
        for key, btn in self.jaws.items():
            btn.configure(text=_fmt(float(pj.get(key, 0))))

    def _info(self, title, definition):
        m = kit.Modal(self.app, title)
        tk.Label(m.body, text=definition, font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=560, justify="left").pack(anchor="w")
        kit.big_button(m.body, "Back", m.close, bg=kit.CARD_HI, fg=kit.FG,
                       width=12, height=2).pack(pady=(10, 0))

    def _edit_height(self, key, label, lo, hi, step):
        """The standard set-value experience: the slider modal."""
        if key == "board":
            def change(v):
                settings.set("arm.board_height", round(v, 2))
                self.refresh_labels()
            kit.slider(self.app, label + "\narm.board_height", lo, hi, step,
                       float(settings.get("arm.board_height", 0.5)), change,
                       confirm_msg="are you sure?")
            return

        def change(v):
            pg = dict(settings.get("arm.piece_grips") or RD.piecegrips)
            pg[key] = round(v, 2)
            settings.set("arm.piece_grips", pg)
            self.refresh_labels()
        cur = dict(settings.get("arm.piece_grips") or RD.piecegrips)
        kit.slider(self.app, label + "\narm.piece_grips." + key, lo, hi,
                   step, float(cur.get(key, 0.0)), change,
                   confirm_msg="are you sure?")

    def _edit_jaw(self, key):
        def change(v):
            pj = dict(settings.get("arm.piece_jaws") or RD.piecejaws)
            pj[key] = round(v, 1)
            settings.set("arm.piece_jaws", pj)
            self.refresh_labels()
        cur = dict(settings.get("arm.piece_jaws") or RD.piecejaws)
        kit.slider(self.app,
                   self.NAMES.get(key, key) + " jaws\narm.piece_jaws." + key,
                   self.JAW_LO, self.JAW_HI, self.JAW_STEP,
                   float(cur.get(key, 0.0)), change,
                   confirm_msg="are you sure?")


# ── board size ───────────────────────────────────────────────────────────────

class BoardSizeScreen(kit.Screen):
    """Board-identity settings (axis-to-row-8, square size, graveyard gap) that
    were crowded out of the main Calibration-settings list. Same styling."""
    def __init__(self, app):
        super().__init__(app=app, title="Board size")
        self.app = app
        self.rows = {}
        mode = settings.get("arm.calib_mode", "A")
        items = [e for e in settings.BOARD_EDITABLE
                 if settings.visible_in_mode(e[0], mode)]
        grid = tk.Frame(self.body, bg=kit.BG)
        grid.pack(fill="both", expand=True)
        for r in range(len(items)):
            grid.rowconfigure(r, weight=1)
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        for i, (key, label, lo, hi, step, definition) in enumerate(items):
            name_cell = tk.Frame(grid, bg=kit.BG)
            name_cell.grid(row=i, column=0, sticky="nsew", pady=4)
            tk.Label(name_cell, text=label, font=("Atkinson Hyperlegible", 11),
                     bg=kit.BG, fg=kit.FG, anchor="w").pack(side="left")
            kit.info_button(name_cell,
                            lambda t=label, d=definition: self._info(t, d),
                            small=True, bg=kit.BG).pack(side="left",
                                                        padx=(4, 0))
            cell = tk.Frame(grid, bg=kit.BG)
            cell.grid(row=i, column=1, sticky="nsew", padx=(8, 0), pady=4)
            btn = kit.big_button(cell, "",
                                 lambda k=key, l=label, lo=lo, hi=hi, st=step:
                                 self._edit(k, l, lo, hi, st),
                                 bg=kit.CARD_HI, fg=kit.ACCENT, width=8,
                                 height=2, font=("Atkinson Hyperlegible", 12))
            btn.pack(fill="both", expand=True)
            self.rows[key] = btn
        self.refresh_labels()

    def refresh_labels(self):
        for key, btn in self.rows.items():
            btn.configure(text=_fmt(settings.get(key)))

    def _info(self, title, definition):
        m = kit.Modal(self.app, title)
        tk.Label(m.body, text=definition, font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=560, justify="left").pack(anchor="w")
        kit.big_button(m.body, "Back", m.close, bg=kit.CARD_HI, fg=kit.FG,
                       width=12, height=2).pack(pady=(10, 0))

    def _edit(self, key, label, lo, hi, step):
        def change(v):
            settings.set(key, v)
            self.refresh_labels()
        kit.slider(self.app, label + "\n" + key, lo, hi, step,
                   settings.get(key), change, confirm_msg="are you sure?")


# ── calibration test ─────────────────────────────────────────────────────────

class CalibrationTestScreen(kit.Screen):
    """Reach/accuracy test: tap a square, tap Go - the arm cruises there and
    grabs (empty board), exactly as a game pick. Repeat for other squares;
    Back parks the arm and returns."""

    # the piece the test simulates PICKING UP (2026-11-14)
    PIECES = ("p", "n", "b", "r", "q", "k")
    PIECE_NAMES = {"p": "pawn", "n": "knight", "b": "bishop",
                   "r": "rook", "q": "queen", "k": "king"}

    def __init__(self, app):
        super().__init__(app=app, title="Calibration test")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.target = None
        RD.STRICT_IK = True   # IK failures raise instead of a silent fallback

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=320)
        cols.set_board(self.board, redraw=self._draw_board)
        self.board.bind("<Button-1>", self._on_tap)
        self._draw_board()

        side = tk.Frame(cols.right, bg=kit.BG)
        side.pack(fill="x")
        self.go_btn = kit.big_button(side, "Go", self._go, bg=kit.GREEN,
                                     fg="#1b1e24", height=2, width=8)
        self.go_btn.pack(anchor="w", pady=(6, 0))

        self.instr_lbl = kit.FitLabel(cols.right, font=("Atkinson Hyperlegible", 15))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self._set_instr()
        # piece buttons (2026-11-14): which piece the simulated pick picks
        # up - pawn selected by default, tapping another piece makes the
        # NEXT move (Go) simulate that one (grip height + jaw width follow)
        self.piece = "p"
        self._piece_btns = {}
        prow = tk.Frame(cols.right, bg=kit.BG)
        prow.pack(pady=(8, 0))
        for i, k in enumerate(self.PIECES):
            b = kit.big_button(prow, kit.GLYPH[k],
                               lambda kk=k: self._set_piece(kk),
                               bg=kit.CARD_HI, fg=kit.FG, width=5, height=2,
                               font=kit.F_PIECE)
            b.grid(row=i // 3, column=i % 3, padx=4, pady=4)
            self._piece_btns[k] = b
        self._set_piece("p", refresh=False)
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()

    def _draw_board(self):
        board = [["."] * 8 for _ in range(8)]
        hl = None
        if self.target:
            col = RD.xtrans[self.target[0]]
            row = 8 - int(self.target[1])
            board[row][col] = self.piece.upper()
            hl = (row, col, row, col)
        self.board.draw(board, hl)

    def _on_tap(self, event):
        col = event.x // self.board.sq
        row = event.y // self.board.sq
        if 0 <= col < 8 and 0 <= row < 8:
            self.target = "abcdefgh"[col] + str(8 - row)
            self._draw_board()
            self._set_instr()
            self.go_btn.configure(state="normal", bg=kit.GREEN, fg="#1b1e24")

    def _set_piece(self, k, refresh=True):
        """The piece the simulated pick picks up (2026-11-14): pawn is
        selected by default; the next Go simulates whatever is selected."""
        self.piece = k
        for kk, b in self._piece_btns.items():
            sel = (kk == k)
            b.configure(bg=kit.ACCENT if sel else kit.CARD_HI,
                        fg=("#1b1e24" if sel else kit.FG),
                        activebackground=(kit.ACCENT_HI if sel
                                          else kit.CARD_HI))
        if refresh:
            self._draw_board()
            self._set_instr()

    def _set_instr(self):
        if self.target:
            self.instr_lbl.configure(
                text="Target %s \u2014 tap Go to move there and pick up "
                     "the %s. Tap another square to change."
                     % (self.target.upper(),
                        self.PIECE_NAMES[self.piece]))
        else:
            self.instr_lbl.configure(
                text="Tap a square on the board, then tap Go.")

    def _go(self):
        if self.busy or self.target is None:
            return
        sq = self.target
        X = RD.armlength(sq)
        Z = RD.armangle(sq)
        Y = RD.grip_y(self.piece, X)
        # Refuse an unreachable target instead of silently falling back to the
        # all-middle default pose (2026-10-14: a broken arm frame made every
        # square unreachable and the arm looked frozen at a default pose).
        if not RD.pose_ok(X, Y, Z):
            self.app.toast("Target %s out of reach - re-run Board calibration"
                           % sq.upper())
            return
        self.busy = True
        self.go_btn.configure(state="disabled", bg=kit.CARD, fg=kit.FG_DIM)

        def run():
            try:
                RD.energise()          # idempotent: wakes only if asleep
                RD.openjaws()
                RD.movearm(X, Z)       # cruise to the square
                RD.moveXY(Y)           # descend as the game picks
                RD.closejaws(self.piece)  # grab at the piece's jaw width
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
                    self.go_btn.configure(state="normal", bg=kit.GREEN,
                                          fg="#1b1e24")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _cleanup(self):
        self.stop = True
        RD.STRICT_IK = False

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        RD.STRICT_IK = False   # park must never fail on a strict check
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


# ── arm sag compensation ─────────────────────────────────────────────────────

def _catmull(pts, n=10):
    """Smooth (Catmull-Rom) polyline through pts - the sag curve is curved,
    not a chain of straight segments."""
    if len(pts) < 2:
        return list(pts)
    p = [pts[0]] + list(pts) + [pts[-1]]
    out = []
    for i in range(len(pts) - 1):
        p0, p1, p2, p3 = p[i], p[i + 1], p[i + 2], p[i + 3]
        for j in range(n):
            t = j / float(n)
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t +
                       (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2 +
                       (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                for k in (0, 1)))
    out.append(tuple(pts[-1]))
    return out


class ArmSagScreen(kit.Screen):
    """'Arm sag compensation': the arm flexes as it extends, so a commanded
    height lands LOW and the droop grows with reach. Each sample square is a
    box on the curve of extension -> compensation (the value ADDED to any
    commanded height at that reach). Tap a box: the arm lifts, goes to that
    square and descends to a pawn's grip height + compensation. Tune the
    value with the arrows until the gripper is at grip height on the pawn -
    the model and the curve update live and the gripper follows."""

    SERIES = ("d8", "d7", "d6", "d5", "d4", "d3", "d2", "d1", "h1")
    CH_W, CH_H = 520, 262
    PAD_L, PAD_R, PAD_T, PAD_B = 46, 14, 26, 34
    BOX_W, BOX_H = 22, 14
    STEP = 0.1
    COMP_LO, COMP_HI = -2.0, 2.0

    def __init__(self, app):
        super().__init__(app=app, title="Arm sag compensation")
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.stop = False
        self.sel = None
        self.saved = False
        self.orig = self._load()
        self.pts = dict(self.orig)
        self.reach = {sq: RD.armlength(sq) for sq in self.SERIES}

        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both", expand=True)
        self.chart = tk.Canvas(row, width=self.CH_W, height=self.CH_H,
                               bg="#181b21", highlightthickness=0)
        self.chart.pack(side="left")
        self.chart.bind("<Button-1>", self._on_tap)
        right = tk.Frame(row, bg=kit.BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        side = tk.Frame(right, bg=kit.BG)
        side.pack(fill="x")
        arrows = tk.Frame(side, bg=kit.BG)
        arrows.pack()
        self.up_btn = kit.big_button(arrows, "\u25B2",
                                     lambda: self._nudge(self.STEP),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN, disabled=True)
        self.up_btn.pack(side="left", padx=(0, 4))
        self.dn_btn = kit.big_button(arrows, "\u25BC",
                                     lambda: self._nudge(-self.STEP),
                                     bg=kit.CARD_HI, fg=kit.FG, width=4,
                                     height=2, font=kit.F_BTN, disabled=True)
        self.dn_btn.pack(side="left")
        self.orig_lbl = tk.Label(side, text="was  -", bg=kit.BG,
                                 fg=kit.FG_DIM, font=("Atkinson Hyperlegible", 13))
        self.orig_lbl.pack(anchor="w", pady=(10, 0))
        self.val_lbl = tk.Label(side, text="now  -", bg=kit.BG, fg=kit.ACCENT,
                                font=("JetBrains Mono", 22, "bold"),
                                anchor="w")
        self.val_lbl.pack(anchor="w")
        self.sel_lbl = tk.Label(side, text="tap a box", bg=kit.BG,
                                fg=kit.FG_DIM, font=kit.F_SMALL, anchor="w")
        self.sel_lbl.pack(anchor="w", pady=(2, 0))
        kit.big_button(side, "Save", self._save, bg=kit.GREEN, fg="#1b1e24",
                       height=2, width=10).pack(pady=(14, 0), anchor="w")

        self.instr_lbl = kit.FitLabel(right, font=("Atkinson Hyperlegible", 15))
        self.instr_lbl.pack(fill="x", pady=(6, 0))
        self.instr_lbl.configure(
            text="Tap a box to adjust the compensation for arm sag at that "
                 "range of extension. Place a pawn under the place where the "
                 "arm lands and adjust value until the gripper is at grip "
                 "height.")
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._poll()
        self._draw()

    # ---- model ----
    def _load(self):
        saved = settings.get("arm.sag_points") or []
        got = {}
        for item in saved:
            try:
                got[str(item[0])] = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
        return {sq: float(got.get(sq, 0.0)) for sq in self.SERIES}

    def _points(self):
        return [[sq, self.pts[sq]] for sq in self.SERIES]

    def _xlim(self):
        xs = [self.reach[sq] for sq in self.SERIES]
        return min(xs) - 0.6, max(xs) + 0.6

    def _xy(self, x, y):
        lo_x, hi_x = self._xlim()
        px = self.PAD_L + (x - lo_x) / (hi_x - lo_x) * (
            self.CH_W - self.PAD_L - self.PAD_R)
        py = self.PAD_T + (self.COMP_HI - y) / (self.COMP_HI - self.COMP_LO) * (
            self.CH_H - self.PAD_T - self.PAD_B)
        return px, py

    # ---- chart ----
    def _draw(self):
        c = self.chart
        c.delete("all")
        lo_x, hi_x = self._xlim()
        y = math.floor(self.COMP_LO / 0.5) * 0.5
        while y <= self.COMP_HI + 1e-6:
            px0, py = self._xy(lo_x, y)
            px1, _ = self._xy(hi_x, y)
            c.create_line(px0, py, px1, py, fill="#262c36")
            c.create_text(px0 - 6, py, text="%.1f" % y, anchor="e",
                          fill=kit.FG_DIM, font=kit.F_SMALL)
            y += 0.5
        x = math.ceil(lo_x)
        while x <= hi_x:
            px, py0 = self._xy(x, self.COMP_LO)
            _, py1 = self._xy(x, self.COMP_HI)
            c.create_line(px, py0, px, py1, fill="#262c36")
            c.create_text(px, py0 + 12, text="%d" % x, anchor="n",
                          fill=kit.FG_DIM, font=kit.F_SMALL)
            x += 2
        c.create_text(self.CH_W - self.PAD_R, self.CH_H - 8,
                      text="extension (reach) in", anchor="e",
                      fill=kit.FG_DIM, font=kit.F_SMALL)
        c.create_text(4, 8, text="compensation added to height (in)",
                      anchor="w", fill=kit.FG_DIM, font=kit.F_SMALL)
        pts = [self._xy(self.reach[sq], self.pts[sq]) for sq in self.SERIES]
        curve = _catmull(pts, 10)
        c.create_line(*[v for p in curve for v in p], fill=kit.ACCENT, width=2)
        for i, sq in enumerate(self.SERIES):
            px, py = pts[i]
            on = (i == self.sel)
            c.create_rectangle(px - self.BOX_W / 2, py - self.BOX_H / 2,
                               px + self.BOX_W / 2, py + self.BOX_H / 2,
                               fill=kit.ACCENT if on else "#2a3140",
                               outline="#ffffff" if on else kit.FG_DIM,
                               width=2 if on else 1)
            c.create_text(px, py - self.BOX_H / 2 - 9,
                          text="%.2f" % self.pts[sq],
                          fill=kit.FG if on else kit.FG_DIM,
                          font=("Atkinson Hyperlegible", 10,
                                "bold" if on else "normal"))
            c.create_text(px, py + self.BOX_H / 2 + 8, text=sq.upper(),
                          fill=kit.FG if on else kit.FG_DIM, font=kit.F_SMALL)

    def _on_tap(self, evt):
        if self.busy:
            return
        hit = None
        for i, sq in enumerate(self.SERIES):
            px, py = self._xy(self.reach[sq], self.pts[sq])
            if (abs(evt.x - px) <= self.BOX_W / 2 + 12 and
                    abs(evt.y - py) <= self.BOX_H / 2 + 12):
                hit = i
                break
        self.sel = hit           # a tap on the chart but not a box = deselect
        self._draw()
        self._show_vals()
        if hit is not None:
            self._goto()

    def _show_vals(self):
        if self.sel is None:
            self.orig_lbl.configure(text="was  -")
            self.val_lbl.configure(text="now  -")
            self.sel_lbl.configure(text="tap a box")
            self._set_arrows(False)
            return
        sq = self.SERIES[self.sel]
        self.orig_lbl.configure(text="was  %.2f" % self.orig[sq])
        self.val_lbl.configure(text="now  %.2f" % self.pts[sq])
        self.sel_lbl.configure(text="%s  reach %.1f in"
                               % (sq.upper(), self.reach[sq]))
        self._set_arrows(True)

    def _set_arrows(self, on):
        for b in (self.up_btn, self.dn_btn):
            b.configure(state="normal" if on else "disabled",
                        bg=kit.CARD_HI if on else kit.CARD,
                        fg=kit.FG if on else kit.FG_DIM)

    # ---- live arm ----
    def _nudge(self, d):
        if self.busy or self.sel is None:
            return
        sq = self.SERIES[self.sel]
        self.pts[sq] = max(self.COMP_LO,
                           min(self.COMP_HI, round(self.pts[sq] + d, 3)))
        RD.set_sag(self._points())     # live: the model updates as we tune
        self._draw()
        self._show_vals()
        self._reheight()

    def _goto(self):
        sq = self.SERIES[self.sel]
        X, Z = self.reach[sq], RD.armangle(sq)
        Y = RD.grip_y("p", X)          # the seam adds the compensation
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                RD.energise()
                RD.movearm(X, Z)
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _reheight(self):
        if self.busy or self.sel is None:
            return
        Y = RD.grip_y("p", self.reach[self.SERIES[self.sel]])
        self.busy = True

        def run():
            try:
                RD.moveXY(Y)
            except Exception:
                pass
            self.q.put(("done",))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                if self.q.get_nowait()[0] == "done":
                    self.busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    # ---- save / cancel ----
    def _save(self):
        self.saved = True
        settings.set("arm.sag_points", self._points())
        self.app.toast("Arm sag compensation saved")
        self._finish()

    def _cleanup(self):
        self.stop = True

    def _finish(self):
        if not self.begin_close():
            return
        self.stop = True
        if not self.saved:
            # Cancel: drop the live tuning and restore the saved curve.
            # BUGFIX (2026-11-07): self.orig is a {square: comp} DICT and
            # set_sag expects [[square, comp], ...] - iterating the dict
            # yielded its KEYS ('d4' -> item[0]='d'...), every sample failed
            # the float parse and the sag was wiped to EMPTY (no
            # compensation at all), not restored.
            RD.set_sag([[sq, v] for sq, v in self.orig.items()])
        if RD.sp is None:
            self.app.back()
            return

        def run():
            try:
                RD.park()
            except Exception:
                pass
            self.app.root.after(0, self.app.back)
        threading.Thread(target=run, daemon=True).start()

    def request_back(self):
        self._finish()


# ── analysis ─────────────────────────────────────────────────────────────────

def proceed_without_voice(app, on_wait, on_text):
    """The player wants to advance but the sound file is still cooking."""
    m = kit.Modal(app, "Proceed without voice?")
    tk.Label(m.body, text="The voice file is still being generated. View "
             "text only or wait for the sound file.", font=kit.F_BODY,
             bg=kit.CARD, fg=kit.FG, wraplength=420,
             justify="left").pack(pady=(0, 10))
    row = tk.Frame(m.body, bg=kit.CARD)
    row.pack()
    kit.big_button(row, "Wait", lambda: (m.close(), on_wait()),
                   bg=kit.CARD_HI, fg=kit.FG, width=14,
                   height=2).pack(side="left", padx=8)
    kit.big_button(row, "Text only", lambda: (m.close(), on_text()),
                   bg=kit.GREEN, fg="#1b1e24", width=14,
                   height=2).pack(side="left", padx=8)

class AnalysisHomeScreen(kit.Screen):
    """Analysis of the LAST FINISHED game: the final position, the date and
    winner, the run options and Begin. Per-move text and audio are generated
    in the background as soon as the page opens - text for every move first,
    then one sound file per move starting with the first."""

    VOICE_KEYS = ("none", "espeak", "alba", "glados")
    DEPTH_KEYS = ("12", "15", "18", "21", "24", "27", "30")

    def _depth_label(self, d):
        """'18 (~19 min)' - the measured Pi 3B cost scaled to this game's
        length (2026-11-08), so the estimate moves with the game."""
        n = (self.gpack.move_count()
             if getattr(self, "gpack", None) is not None else 25)
        return "%d (%s)" % (d, AP.fmt_estimate(
            AP.estimate_text_seconds(d, n)))

    def __init__(self, app):
        super().__init__(app=app, title="Analysis")
        self.app = app
        self.q = queue.Queue()
        self.stop = False
        self.worker = None
        self._dots = 0
        self.gpack = AP.load_pack()
        if self.gpack is None:
            tk.Label(self.body, text="No finished game yet. Finish a game "
                     "(checkmate or End) and come back.", font=kit.F_BODY,
                     bg=kit.BG, fg=kit.FG_DIM, wraplength=520,
                     justify="left").pack(pady=40)
            return
        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both", expand=True, pady=(4, 0))
        left = tk.Frame(row, bg=kit.BG)
        left.pack(side="left", fill="y")
        right = tk.Frame(row, bg=kit.BG)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))
        # the board on top, the evaluation chart under it (2026-10-24) -
        # the chart grows as the analysis advances
        top = tk.Frame(left, bg=kit.BG)
        top.pack(side="top", fill="both", expand=True)
        self.chart = kit.EvalChart(left, w=396, h=100)
        self.chart.pack(side="bottom", pady=(4, 0))
        self._board_col = top
        self._right_col = right
        self.bv = None
        left.bind("<Configure>", self._fit_board)
        top.bind("<Configure>", self._fit_board)

        date = (self.gpack.game.get("started") or "")[:10]
        res = AP.game_from_state(self.gpack.game).headers.get("Result", "*")
        winner = ("White wins" if res == "1-0" else
                  "Black wins" if res == "0-1" else
                  "Draw" if res == "1/2-1/2" else "Unfinished")
        tk.Label(right, text="%s  \u00b7  %s" % (date, winner),
                 font=("Atkinson Hyperlegible", 16, "bold"),
                 bg=kit.BG, fg=kit.ACCENT).pack(anchor="w", pady=(0, 12))

        # the review MODE lives with the other options (2026-10-24): with the
        # engine analysis off the review shows just the moves and the stars,
        # and Begin needs no engine text at all
        self.engine_var = tk.BooleanVar(
            value=bool(settings.get("analysis.show_engine", True)))
        kit.checkbox(right, "Display Engine analysis", self.engine_var,
                     command=self._save_engine,
                     font=("Atkinson Hyperlegible", 16)).pack(anchor="w", pady=(0, 4))
        # touch-sized checkboxes (2026-10-24): drawn boxes - Tk's checkbutton
        # INDICATOR does not grow with the font
        self.adv_var = tk.BooleanVar(
            value=bool(settings.get("analysis.auto_advance", True)))
        kit.checkbox(right, "Auto advance", self.adv_var,
                     command=self._save_adv,
                     font=("Atkinson Hyperlegible", 16)).pack(anchor="w", pady=(0, 4))
        # the Stockfish depth of analysis (2026-10-24): picking a new depth
        # throws out the stored text and audio and starts from scratch
        drow = tk.Frame(right, bg=kit.BG)
        drow.pack(anchor="w", pady=(0, 4))
        tk.Label(drow, text="Engine depth", font=("Atkinson Hyperlegible", 18),
                 bg=kit.BG, fg=kit.FG).pack(side="left", padx=(0, 8))
        # the labels carry the measured cost of each depth for THIS game's
        # length (2026-11-08): '30 (~32 h)' is the honest truth about
        # depth 30 and the estimate follows the game being analysed
        self._depth_labels = [self._depth_label(int(d))
                              for d in self.DEPTH_KEYS]
        cur_d = str(settings.get("analysis.depth", 18))
        if cur_d not in self.DEPTH_KEYS:
            cur_d = "18"
        self.depth_var = tk.StringVar(value=self._depth_label(int(cur_d)))
        dmenu = tk.OptionMenu(drow, self.depth_var, *self._depth_labels,
                              command=self._depth_changed)
        dmenu.configure(font=("Atkinson Hyperlegible", 16), bg=kit.CARD_HI, fg=kit.FG,
                        highlightthickness=0,
                        width=max(len(s) for s in self._depth_labels))
        dmenu["menu"].configure(font=("Atkinson Hyperlegible", 18), bg=kit.CARD_HI,
                                fg=kit.FG, activebackground=kit.ACCENT,
                                activeforeground="#1b1e24")
        dmenu.pack(side="left")
        # the voice setting sits below the checkboxes (2026-10-24), with
        # touch-sized options in the dropdown
        vrow = tk.Frame(right, bg=kit.BG)
        vrow.pack(anchor="w")
        tk.Label(vrow, text="Voice", font=("Atkinson Hyperlegible", 18), bg=kit.BG,
                 fg=kit.FG).pack(side="left", padx=(0, 8))
        cur = AP.norm_voice(settings.get("analysis.voice", AP.DEFAULT_VOICE))
        if cur not in self.VOICE_KEYS:
            cur = "none"
        self.voice_var = tk.StringVar(value=AP.VOICE_LABELS[cur])
        menu = tk.OptionMenu(vrow, self.voice_var,
                             *[AP.VOICE_LABELS[k] for k in self.VOICE_KEYS],
                             command=self._voice_changed)
        menu.configure(font=("Atkinson Hyperlegible", 16), bg=kit.CARD_HI, fg=kit.FG,
                       highlightthickness=0, width=14)
        # the dropdown LIST has its own font - the widget's font never
        # reaches the items (2026-10-24)
        menu["menu"].configure(font=("Atkinson Hyperlegible", 18), bg=kit.CARD_HI,
                               fg=kit.FG, activebackground=kit.ACCENT,
                               activeforeground="#1b1e24")
        menu.pack(side="left")
        # one status line for whatever is generating right now (the stage's
        # own #/# and the animated dots), then the bar
        self.prog_lbl = tk.Label(right, text="Generating text (0/0)...",
                                 font=("Atkinson Hyperlegible", 13), bg=kit.BG,
                                 fg=kit.ACCENT, justify="left", anchor="w",
                                 wraplength=340)
        self.prog_lbl.pack(anchor="w", pady=(6, 0))
        self._prog_parent = right
        self.prog_bar = tk.Canvas(right, width=220, height=16, bg="#181b21",
                                  highlightthickness=0)
        self.prog_bar.pack(anchor="w", pady=(4, 0))
        self._bar_on = True
        self._draw_prog(0.0)

        # Begin lives in the right column, below the options (2026-10-24)
        self.begin_btn = kit.big_button(right, "Begin", self._begin,
                                        bg=kit.GREEN, fg="#1b1e24",
                                        height=2, width=16, disabled=True)
        self.begin_btn.pack(anchor="w", pady=(8, 0))
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._start_generation()
        self._poll()

    # options ---------------------------------------------------------------
    def _voice_key(self):
        label = self.voice_var.get()
        for k, v in AP.VOICE_LABELS.items():
            if v == label:
                return k
        return "none"

    def _save_adv(self):
        settings.set("analysis.auto_advance", bool(self.adv_var.get()))

    def _save_engine(self):
        settings.set("analysis.show_engine", bool(self.engine_var.get()))
        self._update_begin()

    def _update_begin(self):
        """Begin: the moves-only review needs no engine text at all; the full
        review waits for the text. The voice rule is the LOOSER one: missing
        sounds are the 'proceed without voice' modal's business (2026-10-24)."""
        ready = (self.gpack is not None
                 and (not self.engine_var.get() or bool(self.gpack.moves)))
        try:
            if ready:
                self.begin_btn.configure(state="normal", bg=kit.GREEN,
                                         fg="#1b1e24")
            else:
                self.begin_btn.configure(state="disabled", bg=kit.CARD,
                                         fg=kit.FG_DIM)
        except tk.TclError:
            pass

    def _depth_changed(self, _=None):
        """A new depth throws out the stored text and audio and starts over.
        DESTRUCTIVE, so it asks first (2026-11-08): Cancel puts the old
        depth back in the dropdown, OK wipes and restarts."""
        try:
            depth = int(str(self.depth_var.get()).split()[0])
        except ValueError:
            return
        old = int(settings.get("analysis.depth", 18))
        if depth == old:
            return

        def ok():
            settings.set("analysis.depth", depth)
            self._restart_analysis()

        def cancel():
            self.depth_var.set(self._depth_label(old))

        kit.confirm(self.app, "Change analysis depth?",
                    "This will delete your previous analysis and voices.",
                    ok, on_no=cancel, yes_text="OK", danger=True)

    def _restart_analysis(self):
        if self.gpack is not None:
            self.gpack.cancel()
            shutil.rmtree(self.gpack.dir, ignore_errors=True)
            self.gpack.moves = []
            self.gpack.analysis = None
            self.gpack.live_moves = []
            self._start_generation()

    def _voice_changed(self, _=None):
        settings.set("analysis.voice", self._voice_key())
        SP.warm_all()
        # the background generation covers BOTH voices - switching just picks
        # what PLAYBACK uses, nothing to cancel or restart (2026-10-24)
        self._start_generation()

    # generation ------------------------------------------------------------
    def _start_generation(self):
        # join the shared run - the same one idle-time pre-warming uses. Only
        # when no run for THIS voice is live do we cancel an older voice's run
        # (its finished files are kept) and start fresh.
        voice = self._voice_key()
        st = AP.generation_state(self.gpack, voice)
        if st is None or not st.get("alive"):
            self.gpack.cancel()
            AP.generate_async(self.gpack, voice)

    def _fit_board(self, _e=None):
        """The final board fills the entire vertical space of its column
        (2026-10-24), re-fitting if the layout request changes."""
        if not self.winfo_exists():
            return
        h = self._board_col.winfo_height()
        if h < 120:                     # not realized yet
            return
        h -= 12                         # the layout paddings round-trip
        avail = 780 - 12 - self._right_col.winfo_reqwidth()
        px = (max(200, min(h, avail)) // 8) * 8
        if self.bv is not None and self.bv.px == px:
            return
        if self.bv is not None:
            self.bv.destroy()
        self.bv = kit.BoardView(self._board_col, px=px)
        self.bv.pack(anchor="center")   # centred in its column (2026-11-08)
        self.bv.draw(self.gpack.board_after(self.gpack.move_count()).getBoard())

    def _draw_prog(self, frac):
        w = max(2, int(220 * max(0.0, min(1.0, frac))))
        self.prog_bar.delete("all")
        self.prog_bar.create_rectangle(0, 0, w, 16, fill=kit.ACCENT,
                                       outline="")

    def _enable_begin(self):
        self._update_begin()

    def _poll(self):
        voice = self._voice_key()
        st = AP.generation_state(self.gpack, voice) if self.gpack else None
        alive = bool(st and st.get("alive"))
        dots = ""
        if alive:
            self._dots = (self._dots % 3) + 1
            dots = "." * self._dots
        # one line naming the CURRENT stage with the #/# and the animated
        # dots beside it (2026-11-07): the engine analysis first, then the
        # espeak voice, then the piper voice
        if alive and not st.get("error"):
            d, t = st.get("done", 0), st.get("total", 0)
            prog = " (%d/%d)%s" % (d, t, dots)
            if st.get("state") == "audio":
                # BOTH voices generate in sequence (espeak first, then
                # piper) whatever the selection - name the one at work
                av = st.get("voice") or voice
                self.prog_lbl.configure(text="Generating %s voice%s"
                                        % (av, prog))
            else:
                # the estimate travels with the progress (2026-11-08): at
                # depth 30 the run is a day-long affair and 'how much
                # longer' is the one thing worth showing
                left = ""
                if t:
                    per = AP.estimate_text_seconds(
                        settings.get("analysis.depth", 18), 1)
                    left = " - %s left" % AP.fmt_estimate(per * (t - d))
                self.prog_lbl.configure(text="Building analysis%s%s"
                                        % (prog, left))
            self._draw_prog(d / float(max(1, t)))
        elif st is not None and st.get("error"):
            self.prog_lbl.configure(text="Analysis failed: %s" % st["error"])
            self._draw_prog(1.0)
        else:
            self.prog_lbl.configure(text="Ready")
        # Begin only once the TEXT exists - a move screen over an
        # ungenned pack used to fail silently on the button press. The text
        # may land via ANOTHER instance's run (pre-warm): pick it up here.
        if self.gpack is not None and not self.gpack.moves \
                and self.gpack.has_text():
            self.gpack._load_text()
        if st is not None and self.gpack and self.gpack.moves:
            self._enable_begin()
        # the loading bar retires when the run is finished (2026-10-23) and
        # comes back when a new run starts (a voice switch)
        if alive != self._bar_on:
            self._bar_on = alive
            if alive:
                # re-CREATE the bar: re-packing the pack_forget'd canvas
                # left it unmapped in this app's Tk context (2026-11-08) -
                # a fresh canvas maps exactly like the one in __init__
                self.prog_bar = tk.Canvas(self._prog_parent, width=220,
                                          height=16, bg="#181b21",
                                          highlightthickness=0)
                self.prog_bar.pack(anchor="w", pady=(4, 0),
                                   before=self.begin_btn)
                self._draw_prog(0.0)
            else:
                self.prog_bar.pack_forget()
        if alive:
            self.app.hold_awake("analysis")   # generation must not blank the
        else:                                 # panel while the player waits
            self.app.release_awake("analysis")
        # the evaluation chart follows the analysis as it advances; the three
        # turning-point marks wait for the analysis to COMPLETE (they move
        # around while it runs)
        if self.gpack is not None:
            # text that landed on ANOTHER instance's run (the idle prewarm)
            # must still appear here once the run finishes
            if (not self.gpack.moves and not alive and self.gpack.has_text()):
                self.gpack._load_text()
                self._update_begin()
            mv = self.gpack.live_moves or []
            player = self.gpack.game.get("player_color") or "white"
            done = not alive
            self.chart.update_moves(
                mv, stars=self.gpack.game.get("stars") or [],
                turning=(AP.top_turning(mv, player=player) if done else []),
                total=self.gpack.move_count(), player=player)
        if self.winfo_exists() and not self.stop:
            self.after(150, self._poll)

    def _begin(self):
        if not self.gpack or not self.gpack.moves:
            return                      # still generating the text
        voice = self._voice_key()
        first = self.gpack.moves[0] if self.gpack.moves else None
        if first and voice != "none" and \
                not self.gpack.ready(first["ply"], voice):
            proceed_without_voice(
                self.app, on_wait=lambda: None,
                on_text=lambda: self._open(0))
            return
        self._open(0)

    def _open(self, idx):
        if not self.gpack:
            return
        if not self.gpack.moves and self.engine_var.get():
            return                      # the full review needs the text
        # Begin loads the first move already playing (2026-10-22); the review
        # shows the engine analysis per the checkbox (2026-10-24).  Auto
        # advance is driven by ITS OWN checkbox (2026-11-06): it used to pass
        # the engine-analysis checkbox, so unchecking 'Auto advance' did
        # nothing and the review played itself through the whole game.
        self.stop = True
        self.app.release_awake("analysis")
        self.app.show(AnalysisMoveScreen, pack=self.gpack, idx=idx,
                      autoplay=bool(self.adv_var.get()),
                      show_engine=bool(self.engine_var.get()))

    def _cleanup(self):
        self.stop = True
        self.app.release_awake("analysis")

    def request_back(self):
        # back on the analysis HOME goes to the APP home - app.back() would
        # pop into whatever screen opened this one (often the move screen),
        # because the move screen navigates here via show() (2026-10-22)
        self._cleanup()
        self.app.show(HomeScreen)


class AnalysisMoveScreen(kit.Screen):
    """One move of the analysis: the board as of the move (origin and
    destination highlighted), the move text, transport controls and the
    playback progress. Auto advance = the sound + 2 s, or - with no voice -
    scaled to the text (5-10 s). Back returns to the analysis home; the last
    move offers Finish (the app home)."""

    def __init__(self, app, pack, idx, autoplay=False, show_engine=True):
        super().__init__(app=app, title="Analysis")
        self.app = app
        self.gpack = pack
        # the review runs on raw game data too (the moves-only mode before
        # the analysis exists, 2026-10-24)
        if not pack.moves:
            pack.moves = pack.review_moves()
        self.idx = max(0, min(idx, len(pack.moves) - 1))
        self.voice = AP.norm_voice(settings.get("analysis.voice", AP.DEFAULT_VOICE))
        self.adv = bool(settings.get("analysis.auto_advance", True))
        self.stop = False
        self._playing = False
        self._paused = False       # pause freezes the auto-advance counter
        self._timer = None
        self._tick = None
        self._t0 = 0.0
        # the engine overlay can be hidden ('Review without engine') and the
        # engine line can be explored on the board (2026-10-24)
        self._engine_visible = bool(show_engine)
        self._exploring = False
        self._explore_i = 0
        # the what-if board (2026-11-04): test an alternate move from the
        # position before the reviewed move and watch the engine react
        self._testing = False
        self._test_from = None
        self._test_board = None
        self._test_engine = None
        self._test_q = queue.Queue()   # live engine reports (Tk-thread drain)
        self._test_gen = 0             # search generation (stale runs drop)
        self._test_stop = False

        # thin playback progress along the top
        self.bar = tk.Canvas(self.body, height=6, bg="#181b21",
                             highlightthickness=0)
        self.bar.pack(fill="x", pady=(0, 6))
        self.bar.bind("<Configure>", lambda e: self._draw_bar(0.0))

        # two columns: the board takes the entire available height of the
        # left column; the transport buttons sit at the top of the right
        # column with the commentary text filling the rest (2026-10-22)
        cols = tk.Frame(self.body, bg=kit.BG)
        cols.pack(fill="both", expand=True)
        self.cols = cols
        left = tk.Frame(cols, bg=kit.BG)
        left.pack(side="left", fill="y")
        right = tk.Frame(cols, bg=kit.BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self._right_col = right
        # the win bar and the board share the column top; the evaluation
        # chart sits UNDER the board (2026-10-24)
        top = tk.Frame(left, bg=kit.BG)
        top.pack(side="top", fill="both", expand=True)
        self.score_bar = tk.Canvas(top, width=12, bg="#181b21",
                                   highlightthickness=0)
        self.score_bar.pack(side="left", padx=(0, 4))
        self.board = None
        self._board_col = top

        nav = tk.Frame(right, bg=kit.BG)
        nav.pack(fill="x")
        # the transport TRIO FILLS the row (2026-11-08): three equal,
        # stretched buttons across the whole width - big targets for an
        # inaccurate touch board.
        nav.grid_columnconfigure(0, weight=1, uniform="navpad")
        trio = tk.Frame(nav, bg=kit.BG)
        trio.grid(row=0, column=0, sticky="ew")
        for _i in range(3):
            trio.grid_columnconfigure(_i, weight=1, uniform="trio")
        # transport: back / play-pause / advance - 50% taller (2026-11-06)
        # and all EXACTLY the same size (the play button's icon sits over
        # sizing text so it keeps the same text-unit box as the arrows)
        self.back_btn = kit.big_button(trio, "\u25C0", self._prev,
                                       bg=kit.CARD_HI, fg=kit.FG, width=3,
                                       height=3, font=kit.F_BTN)
        self.back_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        # play/pause: PIL-drawn icons (DejaVu Sans has no pause glyph),
        # scaled up for the tall transport buttons
        self._icon_play = kit.glyph_photo("play", size=44)
        self._icon_pause = kit.glyph_photo("pause", size=44)
        self.play_btn = tk.Button(trio, text=" ", command=self._toggle,
                                  bg=kit.GREEN, fg="#1b1e24",
                                  activebackground=kit.GREEN,
                                  activeforeground="#1b1e24", font=kit.F_BTN,
                                  relief="flat", bd=0, highlightthickness=0,
                                  width=3, height=3, compound="center")
        self.play_btn.grid(row=0, column=1, sticky="ew", padx=6)
        self._set_play_glyph(False)
        self.next_btn = kit.big_button(trio, "\u25B6", self._next,
                                       bg=kit.CARD_HI, fg=kit.FG, width=3,
                                       height=3, font=kit.F_BTN)
        self.next_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        self.after_idle(self._match_transport_heights)
        # the quality verdict is a LABEL above the text box (2026-10-23)
        self.verdict_lbl = tk.Label(right, text="", font=("Atkinson Hyperlegible", 16,
                                                          "bold"),
                                    bg=kit.BG, fg=kit.ACCENT, justify="left",
                                    anchor="w", wraplength=340)
        self.verdict_lbl.pack(anchor="w", pady=(8, 0))
        # 'Generating voice...' is shown only while it is needed
        self.gen_lbl = tk.Label(right, text="", font=kit.F_SMALL,
                                bg=kit.BG, fg=kit.ACCENT, justify="center")
        self.txt = tk.Text(right, height=2, width=28, bg="#181b21",
                           fg=kit.FG, font=("Atkinson Hyperlegible", 14), relief="flat",
                           wrap="word", padx=8, pady=6, highlightthickness=0,
                           cursor="arrow")
        self.txt.tag_config("move", font=("Atkinson Hyperlegible", 14, "bold"),
                            foreground=kit.ACCENT)
        self.txt.tag_config("icon", font=("DejaVu Sans", 15))
        self.txt.tag_config("star", foreground="#f6f2e8",
                            font=("DejaVu Sans", 15))
        self.txt.tag_config("turn", foreground="#e08b3e",
                            font=("DejaVu Sans", 15))
        self.txt.pack(fill="both", expand=True, pady=(6, 0))
        self.txt.configure(state="disabled")
        # 'Engine' mode (2026-11-04): the engine's own line, and a what-if
        # board where the player can test an alternate move.  The buttons
        # beneath the engine line form TWO equal columns with equal heights
        # (2026-11-06); Test alternate and Reset share a cell - they swap
        # and are never visible together.
        btns = tk.Frame(right, bg=kit.BG)
        btns.pack(anchor="w", pady=(6, 0))
        btns.grid_columnconfigure(0, weight=1, uniform="mcol")
        btns.grid_columnconfigure(1, weight=1, uniform="mcol")
        # width=14 (was 15): with the terminal strip gone the board column
        # grew taller and the board must not be WIDTH-capped by this grid
        # (test_move_screen, 2026-11-08)
        self.explore_btn = kit.big_button(btns, "Engine",
                                          self._explore_toggle,
                                          bg=kit.CARD_HI, fg=kit.FG,
                                          width=14, height=2,
                                          font=("Atkinson Hyperlegible", 13))
        self.explore_btn.grid(row=0, column=0, padx=(0, 6), pady=3)
        self.test_btn = kit.big_button(btns, "Test alternate",
                                       self._test_start,
                                       bg=kit.CARD_HI, fg=kit.FG,
                                       width=14, height=2,
                                       font=("Atkinson Hyperlegible", 13))
        self.reset_btn = kit.big_button(btns, "Reset", self._test_reset,
                                        bg=kit.CARD_HI, fg=kit.FG,
                                        width=14, height=2,
                                        font=("Atkinson Hyperlegible", 13))
        self.engine_btn = kit.big_button(btns, "Hide analysis",
                                         self._toggle_engine,
                                         bg=kit.CARD_HI, fg=kit.FG,
                                         width=14, height=2,
                                         font=("Atkinson Hyperlegible", 13))
        self.engine_btn.grid(row=0, column=1, padx=6, pady=3)

        # the board fills its column's height once the layout is realized
        left.bind("<Configure>", self._fit_board)
        top.bind("<Configure>", self._fit_board)
        self.bind("<Destroy>", lambda *_: self._cleanup(), add="+")
        self._show()
        if autoplay:
            # Begin loads the first move already playing (2026-10-22)
            self._play()
        elif self.adv:
            self._start_timer()
        self._poll()

    # content ---------------------------------------------------------------
    @property
    def move(self):
        return self.gpack.moves[self.idx]

    def _fit_board(self, _e=None):
        """The board takes the entire available height of its column - but
        never more width than leaves the right column its request. Re-fits
        when that request changes, so the body never overflows the
        window."""
        if not self.winfo_exists():
            return
        h = self._board_col.winfo_height()
        if h < 120:                     # not realized yet
            return
        h -= 12                         # the layout paddings round-trip
        # body inner width is 800 - 2*10 padx; the right column keeps its
        # requested width (buttons / text) plus the 12px column gap and the
        # win-probability bar (12px + gap)
        avail = 780 - 12 - 20 - self._right_col.winfo_reqwidth()
        px = (max(200, min(h, avail)) // 8) * 8
        if self.board is not None and self.board.px == px:
            return
        if self.board is not None:
            self.board.destroy()
        self.board = kit.BoardView(self._board_col, px=px)
        self.board.pack(side="left")
        self.score_bar.configure(height=px)
        self._show_board()

    def _draw_eval(self, m):
        """The POSITION SCORE bar left of the board (2026-10-24): white at
        the bottom, black at the top, split by the engine's evaluation of
        positional strength - the same scale as the chart's axis (±1200)."""
        cp = float(m.get("eval_cp") or 0)
        h = int(self.score_bar.cget("height") or 0) or 200
        ww = int(self.score_bar.cget("width") or 12)
        v = max(-1200.0, min(1200.0, cp))
        wh = int(round(h * (0.5 + 0.5 * v / 1200.0)))
        self.score_bar.delete("all")
        light, dark = "#e8e4da", "#20242c"
        self.score_bar.create_rectangle(0, 0, ww, h - wh,
                                        fill=dark, outline="")
        self.score_bar.create_rectangle(0, h - wh, ww, h,
                                        fill=light, outline="")

    def _show_board(self):
        if self.board is None:
            return
        if self._exploring:
            self._explore_draw()
            return
        m = self.move
        b = self.gpack.board_after(m["ply"])
        u = m.get("uci") or ""
        hl = None
        if len(u) >= 4:
            fc, fr = ord(u[0]) - 97, 8 - int(u[1])
            tc, tr = ord(u[2]) - 97, 8 - int(u[3])
            hl = (fr, fc, tr, tc)
        self.board.draw(b.getBoard(), hl)
        self._draw_eval(m)

    def _stars(self):
        """(player_star, turn_star) for the move on screen (2026-10-24):
        white = the player starred it during the game, orange = one of the
        three moves where the position score changed most."""
        stars = set(self.gpack.game.get("stars") or [])
        turn = set(AP.top_turning(self.gpack.live_moves or []))
        ply = self.move["ply"]
        return (ply in stars, ply in turn)

    def _show(self):
        m = self.move
        self._show_board()
        eng = self._engine_visible
        self.verdict_lbl.configure(text=(m.get("verdict") or "") if eng
                                   else "")
        # Hide analysis (2026-11-06): the move text takes over the space of
        # the evaluation sentence and the engine line - both are UNPACKED,
        # not just emptied, so the text box grows into their rows
        if eng or self._testing:
            self.verdict_lbl.pack(anchor="w", pady=(8, 0), before=self.txt)
        else:
            self.verdict_lbl.pack_forget()
        if self._exploring:
            # the RECOMMENDED LINE comes first (2026-11-08): entering the
            # engine view shows the line at once - it used to show the
            # narrative/board state and the line only once you advanced
            self._explore_draw()
            self.engine_btn.configure(text=("Hide analysis" if eng
                                            else "Show analysis"))
            self.back_btn.configure(
                state="normal" if self.idx > 0 else "disabled",
                bg=kit.CARD_HI if self.idx > 0 else kit.CARD,
                fg=kit.FG if self.idx > 0 else kit.FG_DIM)
            self._draw_bar(0.0)
            return
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        pstar, tstar = self._stars()
        if tstar:
            self.txt.insert("end", "\u2605", ("turn",))
        if pstar:
            self.txt.insert("end", "\u2605", ("star",))
        if tstar or pstar:
            self.txt.insert("end", " ")
        for kind, val in m["segments"]:
            if kind == "icon":
                # black moves use a black piece icon with a WHITE outline
                # (2026-11-06): the filled glyphs vanish on the dark box
                if m.get("color") == "black":
                    ph = kit.piece_photo(val)
                    if ph is not None:
                        self.txt.image_create("end", image=ph,
                                              align="center")
                        self.txt.insert("end", " ")
                        continue
                self.txt.insert("end", val + " ", ("icon",))
            elif kind == "move":
                self.txt.insert("end", val, ("move",))
            elif not eng:
                continue        # the comment itself is engine analysis
            else:
                self.txt.insert("end", val)
        self.txt.configure(state="disabled")
        self.engine_btn.configure(text=("Hide analysis" if eng
                                        else "Show analysis"))
        # 'Explore engine line' is disabled without engine analysis
        if not self._exploring:
            ok = bool(self._line_uci())
            self.explore_btn.configure(
                state="normal" if ok else "disabled",
                bg=kit.CARD_HI if ok else kit.CARD,
                fg=kit.FG if ok else kit.FG_DIM)
        self.back_btn.configure(state="normal" if self.idx > 0 else "disabled",
                                bg=kit.CARD_HI if self.idx > 0 else kit.CARD,
                                fg=kit.FG if self.idx > 0 else kit.FG_DIM)
        self._refresh_next()
        self._draw_bar(0.0)

    def _refresh_next(self):
        if self._exploring:
            ok = self._explore_i < len(self._line_uci())
        else:
            ok = self.idx < len(self.gpack.moves) - 1
        self.next_btn.configure(state="normal" if ok else "disabled",
                                bg=kit.CARD_HI if ok else kit.CARD,
                                fg=kit.FG if ok else kit.FG_DIM)

    def _sound_ready(self):
        return self.gpack.ready(self.move["ply"], self.voice)

    def _draw_bar(self, frac):
        w = max(2, int(self.bar.winfo_width() * max(0.0, min(1.0, frac))))
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, 6, fill=kit.ACCENT, outline="")

    # transport -------------------------------------------------------------
    def _match_transport_heights(self):
        """All three transport buttons the SAME size (2026-11-06): the image
        button sizes to its icon (tk's width/height options are ignored for
        images), so pad it up to the arrow buttons' box - both dimensions.
        Note cget('pady') returns screen units ('1m'), so measure with the
        padding zeroed instead of parsing it."""
        try:
            want_h = max(self.back_btn.winfo_reqheight(),
                         self.next_btn.winfo_reqheight())
            want_w = max(self.back_btn.winfo_reqwidth(),
                         self.next_btn.winfo_reqwidth())
            self.play_btn.configure(pady=0, padx=0)
            bare_h = self.play_btn.winfo_reqheight()
            bare_w = self.play_btn.winfo_reqwidth()
            self.play_btn.configure(
                pady=max(0, (want_h - bare_h + 1) // 2),
                padx=max(0, (want_w - bare_w + 1) // 2))
        except tk.TclError:
            pass

    def _set_play_glyph(self, playing):
        """PIL-drawn play/pause icons; the pause glyph is missing from the
        Pi's DejaVu, so the text fallback uses two filled bars."""
        icon = self._icon_pause if playing else self._icon_play
        if icon is not None:
            # keep the sizing text so the button stays the same size as the
            # arrow buttons (2026-11-06)
            self.play_btn.configure(image=icon, text=" ")
            self.play_btn.image = icon          # keep a reference alive
        else:
            self.play_btn.configure(image="",
                                    text=("\u25AE\u25AE" if playing
                                          else "\u25B6"))

    def _toggle(self):
        if self._playing:
            self._pause()
        else:
            self._play()

    # engine line exploration (2026-10-24) ----------------------------------
    def _line_uci(self):
        """The engine's OWN line: the best play from the end of the previous
        turn - what the player should have moved (2026-11-04).  The FULL
        line (2026-11-08).  The narrator's move dict used to drop the field
        (2026-11-15): read it from the raw analysis too, so the complete
        line is explored even before the next re-analysis lands."""
        mv = self.move
        if mv.get("line_uci"):
            return mv["line_uci"]
        for e in ((self.gpack.analysis or {}).get("moves") or []):
            if e.get("ply") == mv.get("ply") and e.get("line_uci"):
                return e["line_uci"]
        return mv.get("best_uci") or []

    def _explore_toggle(self):
        if self._exploring:
            self._explore_exit()
            return
        if not self._line_uci():
            return                      # no engine analysis to explore
        # entering ENGINE mode pauses the auto-advance (2026-11-06): the
        # tour must not advance away while the player studies the line.
        # Exiting stays paused - Play resumes explicitly.
        self._pause()
        self._exploring = True
        self._explore_i = 0
        self._set_bg("#2c313b")         # lighter grey: this is the ENGINE's
        self.explore_btn.configure(text="Back to game",
                                   bg=kit.ACCENT, fg="#1b1e24")
        self.engine_btn.grid_remove()   # no 'Hide analysis' in engine mode
        self.test_btn.grid(row=0, column=1, padx=6, pady=3)
        self._show()

    def _explore_exit(self):
        """One-button escape back to the actual game."""
        self._stop_test_engine()
        self._testing = False
        self._test_from = None
        try:
            self.board.unbind("<Button-1>")
        except Exception:
            pass
        self.test_btn.grid_remove()
        self.reset_btn.grid_remove()
        self.engine_btn.grid(row=0, column=1, padx=6, pady=3)
        self._exploring = False
        self._set_bg(kit.BG)
        self.explore_btn.configure(text="Engine",
                                   bg=kit.CARD_HI, fg=kit.FG)
        self._show()

    def _set_bg(self, col):
        for w in (self, self.body, self.cols, self._board_col,
                  self._right_col, self.score_bar):
            try:
                w.configure(bg=col)
            except tk.TclError:
                pass

    def _explore_draw(self):
        """The engine's own line on the board, step by step - the best play
        from the end of the previous turn - with the FULL line in the text
        window, the examined move bold (2026-11-04)."""
        m = self.move
        uci = self._line_uci()
        # the line starts BEFORE the player's move: the engine's answer to
        # 'what should have been played'
        b = self.gpack.board_after(max(0, m["ply"] - 1))
        hl = None
        for u in uci[:self._explore_i]:
            if b.addTextMove(u) is False:
                break
            if len(u) >= 4:
                fc, fr = ord(u[0]) - 97, 8 - int(u[1])
                tc, tr = ord(u[2]) - 97, 8 - int(u[3])
                hl = (fr, fc, tr, tc)
        self.board.draw(b.getBoard(), hl)
        # the FULL line in the window (2026-11-08): SAN built from the
        # UCI list - the old best_line window held 6 plies and stopped
        # the exploration right where it gets interesting
        import chess
        nb = chess.Board()
        for u in (self.gpack.game.get("moves")
                  or [])[:max(0, m["ply"] - 1)]:
            try:
                nb.push_uci(u)
            except ValueError:
                break
        pl = []
        for u in uci:
            try:
                mv = chess.Move.from_uci(u)
                pl.append({"san": nb.san(mv),
                           "color": ("white" if nb.turn == chess.WHITE
                                     else "black")})
                nb.push(mv)
            except Exception:
                break
        base, col0 = m.get("no", 1), m.get("color", "white")
        # the whole projected line in the window where the player's move is
        # listed, with the move being examined in bold
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", "Engine line:  ", ("icon",))
        for i, p in enumerate(pl):
            num = base + i // 2
            dots = "." if p.get("color", col0) == "white" else "..."
            tag = ("move",) if i == self._explore_i - 1 else ()
            self.txt.insert("end", "%d%s%s " % (num, dots, p["san"]), tag)
        self.txt.configure(state="disabled")
        if self._explore_i == 0:
            self.verdict_lbl.configure(text="Engine line:")
        elif self._explore_i <= len(pl):
            p = pl[self._explore_i - 1]
            num = base + (self._explore_i - 1) // 2
            dots = "." if p.get("color", col0) == "white" else "..."
            self.verdict_lbl.configure(
                text="Engine line %d/%d: %d%s%s"
                     % (self._explore_i, len(uci), num, dots, p["san"]))
        self._draw_eval(m)
        self._refresh_next()

    def _toggle_engine(self):
        """Hide / display the engine analysis (evaluation, text, audio)."""
        self._engine_visible = not self._engine_visible
        if not self._engine_visible and self._exploring:
            self._explore_exit()
            return
        self._show()

    # the what-if board (2026-11-04) -----------------------------------------
    def _test_start(self):
        """The what-if board opens at the CURRENT move's position - the one
        the screen is showing - and the player branches from there
        (2026-11-06: it used to rewind to before the reviewed move, a whole
        'new board').  The live engine answers from this position
        immediately, and its line is genuinely live - the pre-baked review
        line is hidden while testing."""
        if not self._exploring:
            return
        self._stop_test_engine()
        self._testing = True
        self._test_from = None
        m = self.move
        import chess
        b = chess.Board()
        for u in (self.gpack.game.get("moves") or [])[:m["ply"]]:
            try:
                b.push_uci(u)
            except ValueError:
                break
        self._test_board = b
        self.test_btn.grid_remove()
        self.reset_btn.grid(row=0, column=1, padx=6, pady=3)
        self.board.bind("<Button-1>", self._board_tap)
        side = "White" if b.turn else "Black"
        self.verdict_lbl.configure(text="Your move (%s):" % side)
        self._test_draw()
        # the engine waits for the PLAYER's move (2026-11-08): making a
        # move on the board is what prompts it - it used to analyse the
        # starting position before the player had done anything
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", "Make a move on the board - the engine "
                        "answers after your move.")
        self.txt.configure(state="disabled")

    def _test_draw(self, hl=None):
        import ChessBoard
        cb = ChessBoard.ChessBoard()
        for u in self._test_board.move_stack:
            cb.addTextMove(u.uci())
        self.board.draw(cb.getBoard(), hl)

    def _board_tap(self, ev):
        """Tap a piece, then tap where it would go (2026-11-05). Tapping off
        the board or on another piece of your own cancels the pending move."""
        if not self._testing or self._test_board is None:
            return
        sq = max(1, self.board.sq)
        col, row = ev.x // sq, ev.y // sq
        import chess
        if not (0 <= col < 8 and 0 <= row < 8):
            # off the board: cancel
            self._test_from = None
            self._test_draw()
            return
        to = chess.square(col, 7 - row)

        def hl_of(square):
            r, c = 7 - chess.square_rank(square), chess.square_file(square)
            return (r, c, r, c)
        piece = self._test_board.piece_at(to)
        if self._test_from is None:
            if piece is not None and piece.color == self._test_board.turn:
                self._test_from = to
                self._test_draw(hl=hl_of(to))
            return
        if piece is not None and piece.color == self._test_board.turn:
            # another piece of your own: cancel the pending move
            self._test_from = None
            self._test_draw()
            return
        mv = chess.Move(self._test_from, to)
        if mv not in self._test_board.legal_moves:
            self._test_from = None
            self._test_draw()
            return
        self._test_from = None
        self._test_board.push(mv)
        self._test_draw(hl=hl_of(mv.to_square))
        self._test_info(None)               # reading placeholder
        self._test_engine_run()

    def _test_info(self, rec, depth=None):
        """The engine's reaction: the impact of the move, the projected line
        and the depth it is reading at - updated as the depth progresses."""
        b = self._test_board
        if b is None:
            return
        words = AN._eval_words(rec.get("cp", 0)) if rec else ""
        pv = ""
        if rec and rec.get("pv"):
            import chess
            nb = b.copy()
            parts = []
            for u in rec["pv"][:8]:
                try:
                    mv = chess.Move.from_uci(u)
                    parts.append(nb.san(mv))
                    nb.push(mv)
                except Exception:
                    break
            pv = " ".join(parts)
        self.verdict_lbl.configure(
            text="Engine (depth %s):" % (depth if depth else "..."))
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        lines = []
        if words:
            lines.append("The move leaves the position at %s." % words)
        if pv:
            lines.append("The projected line: " + pv + ".")
        if not lines:
            lines.append("Reading the position...")
        self.txt.insert("end", "  ".join(lines))
        self.txt.configure(state="disabled")

    def _test_engine_run(self):
        """Deepening search on the what-if position, reporting as it goes.
        Reports travel through a QUEUE that _poll drains on the Tk thread
        (2026-11-06): calling self.after() from the engine thread was
        unreliable and the updates silently died - the depth stayed '...'
        and the line never refreshed.  Each run is generation-stamped so a
        superseded search can never overwrite the current one."""
        self._stop_test_engine()
        import chess
        fen = self._test_board.fen()
        self._test_gen = getattr(self, "_test_gen", 0) + 1
        gen = self._test_gen

        def on_info(depth, rec):
            self._test_q.put((gen, depth, rec))

        def run():
            try:
                eng = AN.Engine(multipv=1)
                eng.analyse_live(
                    fen, on_info, max_depth=30,
                    stop=lambda: self._test_stop or self._test_gen != gen)
                eng.close()
            except Exception as e:
                print("test-alternate engine:", e)
        self._test_stop = False
        threading.Thread(target=run, daemon=True).start()

    def _stop_test_engine(self):
        self._test_stop = True

    def _test_reset(self):
        """Back to the initial state - the position the move was played from."""
        self._stop_test_engine()
        self._testing = False
        self._test_from = None
        self.reset_btn.grid_remove()
        self.test_btn.grid(row=0, column=1, padx=6, pady=3)
        self._show()

    def _play(self):
        self._playing = True
        self._paused = False
        self._set_play_glyph(True)
        self._start_timer()
        if (self._engine_visible and self.voice != "none"
                and self._sound_ready()):
            audio.play_wav(self.gpack.wav_path(self.move["ply"], self.voice))
            self._t0 = time.time()
        self._refresh_next()

    def _pause(self):
        self._playing = False
        self._paused = True        # the auto-advance counter must freeze
        self._set_play_glyph(False)
        audio.stop_wav()
        self._stop_timer()
        self._draw_bar(0.0)

    def _stop_timer(self):
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except Exception:
                pass
        self._timer = None

    def _timer_len(self):
        return AP.auto_advance_seconds(
            self.move.get("words", 0),
            self.gpack.sound_len(self.move["ply"], self.voice)
            if self.voice != "none" else 0.0)

    def _start_timer(self):
        self._stop_timer()
        self._timer = self.after(int(self._timer_len() * 1000), self._on_timer)

    def _on_timer(self):
        self._timer = None
        if self._paused:
            return
        if self.voice != "none" and not self._sound_ready():
            self._pending_tick()
            return
        self._advance()

    def _pending_tick(self):
        """The auto-advance wants to move on but the voice is still cooking."""
        if self._paused or self.stop or not self.winfo_exists():
            return
        if not self.gen_lbl.winfo_ismapped():
            self.gen_lbl.pack(pady=(2, 0), before=self.txt)
            self.gen_lbl.configure(text="Generating voice...")
        if self._sound_ready():
            self.gen_lbl.configure(text="")
            self.gen_lbl.pack_forget()
            self._advance()
            return
        self.after(400, self._pending_tick)

    def _advance(self, force=False):
        if self.idx >= len(self.gpack.moves) - 1:
            return
        nxt = self.idx + 1
        if (not force and self.voice != "none"
                and not self.gpack.ready(self.gpack.moves[nxt]["ply"],
                                         self.voice)):
            # the player may look ahead without waiting for the sound
            proceed_without_voice(
                self.app, on_wait=lambda: None,
                on_text=lambda: self._advance(force=True))
            return
        self.idx = nxt
        self._show()
        if self._playing:
            self._play()
        elif self.adv and not self._paused:
            self._start_timer()

    def _prev(self):
        if self._exploring:
            if self._explore_i > 0:
                self._explore_i -= 1
                self._explore_draw()
            return
        if self.idx <= 0:
            return
        self.idx -= 1
        self._show()
        if self._playing:
            self._play()
        elif self.adv and not self._paused:
            self._start_timer()

    def _next(self):
        if self._exploring:
            uci = self._line_uci()
            if self._explore_i < len(uci):
                self._explore_i += 1
                self._explore_draw()
            return
        if self.idx >= len(self.gpack.moves) - 1:
            return
        self._advance()

    # progress / lifecycle --------------------------------------------------
    def _poll(self):
        # live what-if engine reports come via the queue: Tk widget calls
        # must happen on the main thread (the engine runs in a worker)
        if self._testing:
            try:
                while True:
                    gen, depth, rec = self._test_q.get_nowait()
                    if gen == getattr(self, "_test_gen", 0):
                        self._test_info(rec, depth)
            except queue.Empty:
                pass
        if self._playing and self.voice != "none":
            d = self.gpack.sound_len(self.move["ply"], self.voice)
            frac = (time.time() - self._t0) / d if d > 0 else 0.0
            self._draw_bar(frac if frac < 1.0 else 1.0)
        self._refresh_next()
        # a still-cooking voice file must not let the panel fall asleep
        if self.voice != "none" and not self._sound_ready():
            self.app.hold_awake("analysis-move")
        else:
            self.app.release_awake("analysis-move")
        if self.winfo_exists() and not self.stop:
            self.after(200, self._poll)

    def _cleanup(self):
        self.stop = True
        self._stop_test_engine()
        self._stop_timer()
        audio.stop_wav()
        self.app.release_awake("analysis-move")

    def request_back(self):
        self._cleanup()
        self.app.show(AnalysisHomeScreen)


# ── direct input ─────────────────────────────────────────────────────────────

class DirectScreen(kit.Screen):
    def __init__(self, app):
        super().__init__(app=app, title="Calibration settings")
        self.app = app
        self.rows = {}
        # 4 grid columns: name | value+info | name | value+info - the grid
        # guarantees every value box aligns within its column pair.
        mode = settings.get("arm.calib_mode", "A")
        items = [e for e in settings.EDITABLE
                 if settings.visible_in_mode(e[0], mode)
                 and e[0] not in settings.HIDDEN]
        grid = tk.Frame(self.body, bg=kit.BG)
        grid.pack(fill="both", expand=True)
        per_col = (len(items) + 1) // 2
        grid.columnconfigure(2, minsize=6)           # gutter between pairs
        for col in range(4):                          # name | value | name | value
            grid.columnconfigure(col, weight=3 if col % 2 == 0 else 2)
        for r in range(per_col):
            grid.rowconfigure(r, weight=1)           # rows spread over the height
        for i, (key, label, lo, hi, step, definition) in enumerate(items):
            r = i % per_col
            pair = i // per_col
            name_cell = tk.Frame(grid, bg=kit.BG)
            name_cell.grid(row=r, column=pair * 2, sticky="nsew", pady=1)
            tk.Label(name_cell, text=label, font=("Atkinson Hyperlegible", 11),
                     bg=kit.BG, fg=kit.FG, anchor="w").pack(side="left")
            kit.info_button(name_cell,
                            lambda t=label, d=definition: self._info(t, d),
                            small=True, bg=kit.BG).pack(side="left",
                                                        padx=(4, 0))
            cell = tk.Frame(grid, bg=kit.BG)
            cell.grid(row=r, column=pair * 2 + 1, sticky="nsew", padx=(8, 0))
            _test_map = {
                "arm.gfudge": GfudgeScreen,
                "arm.stickout": ReachSettingsScreen,
                "arm.radscale": ReachSettingsScreen,
            }
            tgt = _test_map.get(key)
            if tgt is not None:
                kit.big_button(cell, "\u25B6",
                               lambda t=tgt: app.show(t),
                               bg=kit.CARD, fg=kit.ACCENT, width=3, height=2,
                               font=("Atkinson Hyperlegible", 12)).pack(side="right",
                                                              fill="y",
                                                              padx=(2, 0))
            btn = kit.big_button(cell, "",
                                 lambda k=key, l=label, lo=lo, hi=hi, st=step:
                                 self._edit(k, l, lo, hi, st),
                                 bg=kit.CARD_HI, fg=kit.ACCENT, width=5,
                                 height=2, font=("Atkinson Hyperlegible", 12))
            btn.pack(side="left", fill="both", expand=True)
            self.rows[key] = btn
        bottom = tk.Frame(self.body, bg=kit.BG)
        bottom.pack(side="bottom", fill="x")
        # Full-width nav buttons (2026-10-21): every button stretches to fill
        # the screen width, the row is DOUBLE height and the label wraps inside
        # the button so two-line names fit. "Board size" is hidden here.
        for label, tgt in (("Board & Pieces", PieceHeightScreen),
                           ("Manual corners", ManualCalibScreen),
                           ("Calibration test", CalibrationTestScreen),
                           ("Arm sag", ArmSagScreen)):
            kit.big_button(bottom, label, lambda t=tgt: app.show(t),
                           bg=kit.CARD, fg=kit.FG, width=8, height=4,
                           font=kit.F_BODY, wraplength=150).pack(
                side="left", expand=True, fill="x", padx=3, pady=(4, 2))
        self.refresh_labels()

    def refresh_labels(self):
        for key, btn in self.rows.items():
            btn.configure(text=_fmt(settings.get(key)))

    def _info(self, title, definition):
        m = kit.Modal(self.app, title)
        tk.Label(m.body, text=definition, font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=560, justify="left").pack(anchor="w")
        kit.big_button(m.body, "Back", m.close, bg=kit.CARD_HI, fg=kit.FG,
                       width=12, height=2).pack(pady=(10, 0))

    def _edit(self, key, label, lo, hi, step):
        def change(v):
            settings.set(key, v)
            self.refresh_labels()
        kit.slider(self.app, label + "\n" + key, lo, hi, step,
                   settings.get(key), change, confirm_msg="are you sure?")


# ── settings ─────────────────────────────────────────────────────────────────

class SettingsScreen(kit.Screen):
    def __init__(self, app):
        super().__init__(app=app, title="Settings")
        self.app = app
        col = tk.Frame(self.body, bg=kit.BG)
        col.pack(expand=True)
        self.level_btn = kit.btn(col, self._level_txt(), self._level,
                                        size="m", kind="secondary", width_px=400)
        self.level_btn.pack(pady=2)
        self.vol_btn = kit.btn(col, self._vol_txt(), self._volume,
                                      size="m", kind="secondary", width_px=400)
        self.vol_btn.pack(pady=2)
        self.mute_btn = kit.btn(col, self._mute_txt(), self._mute,
                                       size="m", kind="secondary",
                                       width_px=400)
        self.mute_btn.pack(pady=2)
        # the spoken voice (2026-11-14): the analysis narration AND the
        # game phrases (hello / check / checkmate / stalemate) speak in it
        self.voice_btn = kit.btn(col, self._voice_txt(), self._voice,
                                        size="m", kind="secondary", width_px=400)
        self.voice_btn.pack(pady=2)
        # Screen rotation (2026-11-14): flips the panel AND the touch
        # matrix together, live - it used to be a kiosk-script edit
        self.rot_btn = kit.btn(col, self._rot_txt(), self._rotate,
                                      size="m", kind="secondary", width_px=400)
        self.rot_btn.pack(pady=2)
        # after all the buttons: the one switch for the terminal-output
        # element, which is hidden everywhere by default (2026-11-08)
        self.term_var = tk.BooleanVar(
            value=bool(settings.get("ui.show_terminal", False)))
        kit.checkbox(col, "Show terminal output", self.term_var,
                     command=self._save_term,
                     font=kit.F_BODY).pack(anchor="w", pady=(2, 0))
        self._refresh()

    def _level_txt(self):
        v = int(settings.get("stockfish.level", 5))
        # the parenthesis is the exact UCI_Elo the engine gets (2026-10-24)
        return "Stockfish level: %d (~%d)" % (v, LV.level_elo(v))

    def _vol_txt(self):
        return "Volume: %d %%" % settings.get("audio.volume", 80)

    def _mute_txt(self):
        return ("Sound: MUTED" if settings.get("audio.muted", False)
                else "Sound: ON")

    def _refresh(self):
        self.level_btn.configure(text=self._level_txt())
        self.vol_btn.configure(text=self._vol_txt())
        self.mute_btn.configure(text=self._mute_txt())
        self.rot_btn.configure(text=self._rot_txt())
        self.voice_btn.configure(text=self._voice_txt())

    def _level(self):
        def setv(v):
            settings.set("stockfish.level", int(v))
            self._refresh()
        kit.slider(self.app, "Stockfish strength level", 1, 10, 1,
                   settings.get("stockfish.level", 5), setv, fmt="%d",
                   extra=lambda v: " (~%d)" % LV.level_elo(int(v)))

    def _save_term(self):
        settings.set("ui.show_terminal", bool(self.term_var.get()))
        self.app.apply_terminal()

    def _volume(self):
        def setv(v):
            settings.set("audio.volume", int(v))
            audio.apply_volume()
            self._refresh()
        kit.slider(self.app, "Volume", 0, 100, 5,
                   settings.get("audio.volume", 80), setv, fmt="%d", unit=" %")

    def _mute(self):
        settings.set("audio.muted",
                     not settings.get("audio.muted", False))
        audio.apply_volume()
        self._refresh()

    def _voice_txt(self):
        return "Voice: " + AP.VOICE_LABELS[AP.norm_voice(
            settings.get("analysis.voice", AP.DEFAULT_VOICE))]

    def _voice(self):
        cur = AP.norm_voice(settings.get("analysis.voice", AP.DEFAULT_VOICE))

        def pick(v):
            settings.set("analysis.voice", v)
            self._refresh()
            SP.warm_all()     # the picked voice first, the rest behind it
        kit.choice(self.app, "Voice",
                   [(AP.VOICE_LABELS[k], k) for k in AP.VOICE_KEYS],
                   pick, current=cur)

    def _rot_txt(self):
        return ("Screen rotation: 180\u00b0"
                if settings.get("display.rotation", "normal") == "inverted"
                else "Screen rotation: Normal")

    def _rotate(self):
        from chessapp import display as DISP
        new = ("normal"
               if settings.get("display.rotation", "normal") == "inverted"
               else "inverted")
        settings.set("display.rotation", new)
        DISP.apply(new)      # live: panel and touch flip together
        self._refresh()


# ── new game ─────────────────────────────────────────────────────────────────

class NewGameScreen(kit.Screen):
    def __init__(self, app):
        super().__init__(app=app, title="New Game")
        self.app = app
        col = tk.Frame(self.body, bg=kit.BG)
        col.pack(expand=True)
        self.lbls = {}
        for name, key in (("Level", "stockfish.level"),
                          ("I play", "player.color")):
            row = tk.Frame(col, bg=kit.BG)
            row.pack(pady=4)
            tk.Label(row, text=name + ":", font=kit.F_BODY, bg=kit.BG,
                     width=10, anchor="e").pack(side="left")
            # the VALUE lives on the button itself (2026-10-24) - a label
            # beside it ran off the edge
            btn = kit.big_button(row, self._txt(key),
                                 lambda k=key: self._edit(k),
                                 bg=kit.CARD_HI, fg=kit.FG, width=15,
                                 height=2, font=kit.F_BODY)
            btn.pack(side="left", padx=6)
            self.lbls[key] = btn
        kit.big_button(col, "BEGIN", self._begin, bg=kit.GREEN, fg="#14161c",
                       height=3, width=22).pack(pady=16)
        self._refresh()

    def _txt(self, key):
        v = settings.get(key)
        if key == "stockfish.level":
            # just [level] ([elo]) - the row's label names it
            return "%d (~%d)" % (int(v), LV.level_elo(int(v)))
        return v.capitalize()

    def _refresh(self):
        for k, b in self.lbls.items():
            b.configure(text=self._txt(k))

    def _edit(self, key):
        if key == "stockfish.level":
            def setv(v):
                settings.set(key, int(v))
                self._refresh()
            kit.slider(self.app, "Stockfish level", 1, 10, 1,
                       settings.get(key), setv, fmt="%d",
                       extra=lambda v: " (~%d)" % LV.level_elo(int(v)))
        else:
            def setv(v):
                settings.set(key, v)
                self._refresh()
            kit.choice(self.app, "You play as",
                       [("White", "white"), ("Black", "black")], setv,
                       current=settings.get(key))

    def _begin(self):
        msg = "Is the physical board set up to the starting position?"
        if game_state.exists() and game_state.load().get("moves"):
            msg += "\n\nStarting a new game will replace the saved game."

        def begin():
            # "New game" is the ONLY truly fresh path: clear any saved game up
            # front so GameScreen's auto-resume (see _worker) has nothing to
            # resume and starts from the initial position. The game being
            # replaced is preserved as the last game for the Analysis module.
            game_state.save_last()
            game_state.clear()
            self.app.show(GameScreen, resume=False)
        kit.confirm(self.app, "Begin?", msg, begin, yes_text="Begin")


# ── game ────────────────────────────────────────────────────────────────────

def _camera_present():
    """Quick open+close probe: has the capture device (re)appeared?  Lets the
    game recover when the camera is re-plugged mid-game."""
    try:
        cap = camera.open_camera(wait=2.0)
    except Exception:
        return False
    if cap is None:
        return False
    camera.close_camera(cap)
    return True


class GameScreen(kit.Screen):
    BACK = False          # no back arrow mid-game: use Menu (abandons cleanly)

    def __init__(self, app, resume=False):
        super().__init__(app=app, title="")
        self.app = app
        self.resume = resume
        self.q = queue.Queue()
        self.stop_req = False
        self.last = None
        self.game = ChessGame(use_robot=True,
                              ask_promotion=self._ask_promotion,
                              on_promo_swap=self._confirm_swap,
                              on_status=lambda s: self.q.put(("toast", s)),
                              on_player_move=lambda mv: self.q.put(
                                  ("plyr", mv)),
                              on_computer_move=lambda mv: self.q.put(
                                  ("comp", mv)))
        self.head_right = None
        self._prep = None
        self._cam_lost = None
        self._takeback_req = False
        self._armed = False
        self._cleaned = False
        self._worker_done = threading.Event()
        # the raw monitor stream - terminal output, hidden by default like
        # the app-wide strip (2026-11-08).  The THINKING dots below still
        # read from the same events even when the line is not shown.
        self.status_lbl = tk.Label(self.body, text="", font=kit.F_SMALL,
                                   bg=kit.BG, fg=kit.FG_DIM, anchor="w",
                                   wraplength=770)
        if settings.get("ui.show_terminal", False):
            self.status_lbl.pack(side="bottom", fill="x", pady=(2, 0))
        row = tk.Frame(self.body, bg=kit.BG)
        row.pack(fill="both", expand=True)
        self.board = kit.BoardView(row, px=384)
        self.board.pack(side="left", padx=(0, 12))
        side = tk.Frame(row, bg=kit.BG)
        side.pack(side="left", fill="y", expand=True)
        self.turn_lbl = tk.Label(side, text="Starting...", font=kit.F_TITLE,
                                 bg=kit.BG, fg=kit.FG, wraplength=300)
        self.turn_lbl.pack(anchor="w", pady=(0, 6))
        # robot colour (player picks theirs) drives the "<COLOR>'S MOVE" title,
        # and a THINKING-dots flag for the read/interpret phase.
        self._robot_white = (settings.get("player.color", "white") == "black")
        self._thinking = False
        self._think_n = 0
        self.moves_txt = tk.Text(side, width=26, height=14, bg=kit.CARD,
                                 fg=kit.FG, relief="flat", font=kit.F_BODY,
                                 state="disabled", highlightthickness=0)
        self.moves_txt.pack(anchor="w")
        # the move list scrolls by finger drag once it outgrows the space
        self.moves_txt.bind("<Button-1>", self._drag_start)
        self.moves_txt.bind("<B1-Motion>", self._drag_move)
        # Menu and Star move sit BELOW the move list (2026-10-24); height 2
        # like the other action buttons on this screen (2026-11-08)
        brow = tk.Frame(side, bg=kit.BG)
        brow.pack(anchor="w", pady=(10, 0))
        self.star_btn = kit.big_button(brow, "\u2606 Star move",
                                       self._toggle_star, bg=kit.CARD_HI,
                                       fg=kit.FG, width=10, height=2)
        self.star_btn.pack(side="left")
        kit.big_button(brow, "Menu", self._menu, bg=kit.CARD_HI, fg=kit.FG,
                       width=8, height=2).pack(side="left", padx=(8, 0))
        threading.Thread(target=self._worker, daemon=True).start()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._poll()

    def on_show(self):
        pass

    def _robot_move_txt(self):
        return ("WHITE'S" if self._robot_white else "BLACK'S") + " MOVE"

    def _start_thinking(self):
        if getattr(self, "_thinking", False):
            return
        self._thinking = True
        self._tick_thinking()

    def _stop_thinking(self):
        self._thinking = False

    def _tick_thinking(self):
        if not getattr(self, "_thinking", False):
            return
        try:
            if not self.winfo_exists():
                return
            self._think_n = self._think_n % 3 + 1
            self.turn_lbl.configure(text="READING" + "." * self._think_n)
            self.after(400, self._tick_thinking)
        except tk.TclError:
            self._thinking = False

    # worker thread ----------------------------------------------------------

    def _worker(self):
        q = self.q
        q.put(("prep", "Preparing the game"))   # before arm wake + engine
                                                # boot - never a bare screen
        armed = False
        try:
            try:
                armed = RD.arm_open()   # just detect the SSC-32U (opens the
                if not armed:           # port + horn trims) - do NOT wake yet
                    q.put(("toast", "SSC-32U not found - robot will only "
                                    "move on the on-screen board"))
                # Arm stays asleep until the first move; movepiece() ->
                # energise() -> hold_ready() performs the gentle wake on demand.
            except Exception as e:
                q.put(("toast", "Arm power-up failed: %s" % e))
            try:
                self.game.start_engine()
                # Always resume when a saved game exists: leaving mid-game to
                # calibrate and navigating BACK re-instantiates this screen,
                # and a fresh new_game() here used to wipe the save (2026-09).
                # "New game" clears the save first, so auto-resume starts fresh.
                self.game.new_game(resume=True)
            except Exception as e:
                q.put(("fatal", str(e)))
                return
            q.put(("board",))   # paint the resumed position + move list
                                # immediately (was blank until robot moved)
            # If the robot is white (human chose black) it may owe the first
            # move - on a fresh game, or a resume where it's white to move.
            color = settings.get("player.color", "white")
            white_to_move = (len(self.game.movelist) % 2 == 0)
            robot_is_white = (color == "black")
            if white_to_move == robot_is_white:   # robot owes the next move
                q.put(("turn", "robot"))
                res = self.game.engine_first_move()
                self.last = res.get("computer_move")
                q.put(("board",))
                if res["game_over"]:
                    q.put(("over", res["result_text"], res.get("result_code")))
                    return
            while not self.stop_req and not self.game.game_over:
                # The camera needs several motionless seconds to build its
                # "before" reference.  Telling the player "YOUR MOVE" before
                # that let moves made during the wait become part of the
                # reference and vanish (2026-09-18). The prep modal holds
                # them until the monitor says ready via the status callback.
                q.put(("prep", "READING"))
                r = self.game.wait_player_move(
                    stop=lambda: self.stop_req or self._takeback_req,
                    status=lambda t: q.put(("stat", t)),
                    on_ready=lambda: q.put(("turn", "you")))
                if self._takeback_req:
                    self._takeback_req = False
                    n = self.game.takeback()
                    q.put(("board",))
                    q.put(("turn", "you"))
                    if n <= 0:
                        q.put(("toast", "Nothing to take back"))
                    continue
                if self.stop_req:
                    break
                if r == "JOSTLE":
                    ok = self.game.handle_jostle()
                    q.put(("toast", "Board moved - " +
                           ("recalibrated, move again" if ok
                            else "could not recover; use Calibrate")))
                    continue
                if r == "NOCAL":
                    q.put(("boardlost",))
                    continue
                if r == "NOCAM":
                    # camera not detected: show the searching modal, then
                    # keep probing until it reappears (or the user goes Home)
                    q.put(("camlost",))
                    while not self.stop_req:
                        time.sleep(2)
                        if _camera_present():
                            break
                    if not self.stop_req:
                        q.put(("camok",))
                    continue
                if r == "m    ":
                    q.put(("toast", "Move not recognised - undo it, centre "
                                    "the pieces on their squares, and play "
                                    "again when it says YOUR MOVE"))
                    continue
                if not r or not r[1:5].strip():
                    continue
                q.put(("turn", "robot"))
                res = self.game.submit_player_move(r)
                if res.get("robot_error"):
                    q.put(("toast", "Robot problem: " + res["robot_error"]))
                if res["error"] and not res["accepted"]:
                    q.put(("illegal", res["error"], res.get("player_move", "")))
                    continue
                self.last = res.get("computer_move") or res.get("player_move")
                q.put(("board",))
                if res["game_over"]:
                    q.put(("over", res["result_text"], res.get("result_code")))
                    break
        except Exception as e:            # was: silent thread death with a
            q.put(("fatal", str(e)))      # frozen game screen (2026-09-18)
        finally:
            # the ARM IS LEFT ALONE here (2026-11-14): at checkmate it
            # stays exactly where it is - the park/idle used to jerk it
            # the moment the result landed.  _cleanup poses and releases
            # it when the player actually LEAVES the screen.
            self._armed = armed
            self._worker_done.set()
            try:
                self.game.shutdown_engine()
            except Exception:
                pass
            q.put(("ended", None))

    # main thread ------------------------------------------------------------

    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "prep":
                    self._stop_thinking()
                    self.turn_lbl.configure(text="Please wait...")
                    self._open_prep(ev[1] if len(ev) > 1 else None)
                elif kind == "stat":
                    self.status_lbl.configure(text=str(ev[1]))
                    # the monitor only says "reading your move" once it has
                    # detected the human's motion -> THINKING (dots) through the
                    # settle/interpret phase until the move is accepted.
                    if "reading your move" in str(ev[1]).lower():
                        self._start_thinking()
                elif kind in ("plyr", "comp"):
                    # moves appear the instant they are accepted: the
                    # player's while the engine thinks, the computer's
                    # BEFORE the arm physically plays it
                    self._stop_thinking()
                    self.last = ev[1]
                    try:
                        self.board.draw(self.game.board(), _hl(self.last))
                        self._refresh_moves()
                    except Exception as e:
                        print("GameScreen: board draw failed:", e)
                    self.turn_lbl.configure(text=self._robot_move_txt())
                elif kind == "askpromo":
                    ev[1]()          # open the promotion prompt (Tk thread)
                elif kind == "promo_swap":
                    ev[1]()          # the swap-the-piece prompt (Tk thread)
                elif kind == "turn":
                    self._close_prep()
                    self._stop_thinking()
                    if not getattr(self, "_greeted", False):
                        self._greeted = True
                        SP.say("hello")   # "Hello. Let's play chess."
                    self.turn_lbl.configure(
                        text="YOUR MOVE" if ev[1] == "you"
                        else self._robot_move_txt())
                    if ev[1] == "you":
                        AP.prewarm()   # the player's turn is idle time
                elif kind == "board":
                    try:
                        self.board.draw(self.game.board(),
                                        _hl(self.last) if self.last else None)
                        self._refresh_moves()
                    except Exception as e:
                        print("GameScreen: board draw failed:", e)
                elif kind == "toast":
                    self.app.toast(str(ev[1]))
                elif kind == "fatal":
                    self._close_prep()
                    self._stop_thinking()
                    self.turn_lbl.configure(text="Engine failed:\n" + ev[1])
                elif kind == "over":
                    self._close_prep()
                    self._stop_thinking()
                    hl = _over_headline(ev[2], ev[1])
                    self.turn_lbl.configure(text=hl)
                    self._result_modal(hl)
                elif kind == "boardlost":
                    self._boardlost_modal()
                elif kind == "illegal":
                    self._illegal_modal(ev[1], ev[2])
                elif kind == "camlost":
                    self._camera_lost_modal()
                elif kind == "camok":
                    self._camera_lost_close()
                elif kind == "ended":
                    self._close_prep()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _on_destroy(self, e):
        if e.widget is self:
            self._cleanup()

    def _cleanup(self):
        """Screen exit: pose the arm and drop torque - AFTER the worker is
        done with it, so the two never fight over the serial port.  The
        game itself never moves the arm on ending (2026-11-14)."""
        if self._cleaned:
            return
        self._cleaned = True
        self.stop_req = True

        def run():
            try:
                self._worker_done.wait(timeout=30)
                if self._armed:
                    if getattr(RD, "energised", False):
                        RD.park()
                    RD.idleall()
                    if RD.sp:
                        RD.sp.close()
                        RD.sp = None
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _result_modal(self, headline):
        """Game over: offer Home or Analysis - the finished game is preserved
        for the analysis module either way."""
        m = kit.Modal(self.app, headline)

        def to_analysis():
            m.close()
            game_state.save_last()
            self.app.show(AnalysisHomeScreen)

        def to_home():
            m.close()
            game_state.save_last()
            self.app.show(HomeScreen)

        # stacked = ONE width (2026-11-07)
        kit.btn_stack(m.body, [
            ("Home", to_home, "secondary"),
            ("Analysis", to_analysis, "confirm"),
        ])[0].pack(pady=6)

    def _refresh_moves(self):
        self.moves_txt.configure(state="normal")
        self.moves_txt.delete("1.0", "end")
        stars = set(self.game.stars)
        sans = game_state.sans_of(self.game.movelist)   # '+', '#' included
        for i in range(0, len(self.game.movelist), 2):
            pair = sans[i:i + 2]

            def cell(txt, ply):
                if not txt:
                    return ""
                return txt + ("\u2605" if ply in stars else "")
            self.moves_txt.insert("end", "%2d. %-7s %-7s\n" % (
                i // 2 + 1, cell(pair[0], i + 1),
                cell(pair[1], i + 2) if len(pair) > 1 else ""))
        self.moves_txt.see("end")
        self.moves_txt.configure(state="disabled")
        # the star button reflects the CURRENT move (the last played)
        ply = len(self.game.movelist)
        on = ply in stars and ply > 0
        self.star_btn.configure(
            text=("\u2605 Starred" if on else "\u2606 Star move"),
            bg=(kit.ACCENT if on else kit.CARD_HI),
            fg=("#1b1e24" if on else kit.FG),
            state="normal" if ply > 0 else "disabled")

    def _toggle_star(self):
        self.game.toggle_star()
        self._refresh_moves()

    def _drag_start(self, e):
        self._drag_y = e.y

    def _drag_move(self, e):
        """Finger-drag scrolling for the move list (2026-10-24)."""
        dy = getattr(self, "_drag_y", e.y) - e.y
        self._drag_y = e.y
        try:
            self.moves_txt.yview_scroll(int(dy / 8), "units")
        except tk.TclError:
            pass

    # menu --------------------------------------------------------------------

    def _camera_lost_modal(self):
        if self._cam_lost is not None:
            return
        m = kit.Modal(self.app, "Camera not found")
        self._cam_lost = m
        tk.Label(m.body, text="No camera is detected. Check your "
                 "connections and try again.", font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=380, justify="center").pack(
            anchor="w", pady=(4, 0))
        self._cam_dots = tk.Label(m.body, text=".", font=kit.F_TITLE,
                                  bg=kit.CARD, fg=kit.ACCENT)
        self._cam_dots.pack()
        self._cam_dots_n = 0
        self._tick_cam_dots()

        def go_home():
            self._cam_lost = None
            m.close()
            self.abandon()
            self.app.show(HomeScreen)
        kit.big_button(m.body, "Go home", go_home, bg=kit.ACCENT, width=12,
                       height=2).pack(pady=(10, 0))

    def _tick_cam_dots(self):
        if self._cam_lost is None:
            return
        try:
            self._cam_dots_n = self._cam_dots_n % 3 + 1
            self._cam_dots.configure(text="." * self._cam_dots_n)
        except tk.TclError:
            self._cam_lost = None
            return
        if self.winfo_exists():
            self.after(400, self._tick_cam_dots)

    def _camera_lost_close(self):
        m = self._cam_lost
        self._cam_lost = None
        if m is not None:
            try:
                m.close()
            except Exception:
                pass

    def _illegal_modal(self, error, mv):
        """The player moved illegally (2026-11-04): name the move and ask for
        the board to be reset. The engine's board never changed - the
        physical pieces no longer match it, so nothing continues until the
        player says the board is back.  Copy kept short (2026-11-06)."""
        if getattr(self, "_illegal_m", None) is not None:
            return
        m = kit.Modal(self.app, "Illegal Move")
        self._illegal_m = m
        try:
            san = game_state.sans_of([mv])[0] if mv else ""
        except Exception:
            san = mv or ""
        said = ("Invalid move in %s." % san) if san else "Invalid move."
        tk.Label(m.body,
                 text=said + "\nPlease reset the board, then press "
                      "\"Continue\".",
                 font=kit.F_BODY, bg=kit.CARD, fg=kit.FG,
                 wraplength=400, justify="left").pack(anchor="w")

        def ok():
            self._illegal_m = None
            m.close()
        kit.big_button(m.body, "Continue", ok,
                       bg=kit.GREEN, fg="#1b1e24", width=14,
                       height=2).pack(pady=(12, 0))

    def _boardlost_modal(self):
        if getattr(self, "_lost_m", None) is not None:
            return
        m = kit.Modal(self.app, "Board not found")
        self._lost_m = m
        tk.Label(m.body, text="The camera can no longer see the board "
                 "where it was calibrated. Something moved - often the "
                 "camera during a hardware fix.\n\nRun Camera calibration "
                 "before continuing.", font=kit.F_BODY, bg=kit.CARD,
                 fg=kit.FG, wraplength=400, justify="left").pack(anchor="w")
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack(pady=(10, 0))

        def wait_more():
            self._lost_m = None
            m.close()
        kit.big_button(row, "Keep waiting", wait_more, bg=kit.CARD_HI,
                       fg=kit.FG, width=13, height=2).pack(side="left",
                                                           padx=6)

        def go_calibrate():
            self._lost_m = None
            m.close()
            self.abandon()
            self.app.show(CalibrateScreen)
        kit.big_button(row, "Calibrate now", go_calibrate, bg=kit.ACCENT,
                       width=14, height=2).pack(side="left", padx=6)

    def _open_prep(self, text=None):
        if self._prep is not None:
            if text:
                try:
                    self._prep_main.configure(text=text)
                except tk.TclError:
                    self._close_prep()
            return
        m = kit.Modal(self.app, "Please wait")
        self._prep_main = tk.Label(m.body,
                                   text=text or "Preparing the game",
                                   font=kit.F_TITLE,
                                   bg=kit.CARD, fg=kit.FG, wraplength=340,
                                   justify="center")
        self._prep_main.pack(pady=(2, 0))
        self._prep_dots = tk.Label(m.body, text=".", font=kit.F_TITLE,
                                   bg=kit.CARD, fg=kit.ACCENT)
        self._prep_dots.pack()

        def go_home():
            # Escape the wait (reading the board / preparing): abandon the
            # turn but KEEP the saved game so it can resume from PLAY.
            self._close_prep()
            self.abandon()
            self.app.show(HomeScreen)
        kit.btn(m.body, "Home", go_home, size="m",
                kind="secondary").pack(pady=(10, 0))
        self._prep = m
        self._prep_n = 0
        self._tick_prep()

    def _tick_prep(self):
        if self._prep is None:
            return
        try:
            self._prep_n = self._prep_n % 3 + 1
            self._prep_dots.configure(text="." * self._prep_n)
        except tk.TclError:
            self._prep = None
            return
        if self.winfo_exists():
            self.after(500, self._tick_prep)

    def _close_prep(self):
        m = self._prep
        self._prep = None
        if m is not None:
            try:
                m.close()
            except Exception:
                pass

    def _menu(self):
        handlers = {
            "home": lambda: kit.confirm(
                self.app, "Home", "Leave the game? It stays saved - resume "
                "from PLAY.", lambda: (self.abandon(), self.app.show(HomeScreen))),
            "new": lambda: kit.confirm(
                self.app, "New game", "The current game will be lost.",
                lambda: (self.abandon(), self.app.show(NewGameScreen))),
            "end": lambda: self._end_modal(),
            "takeback": lambda: self._takeback(),
            "calib": lambda: kit.confirm(
                self.app, "Calibration", "Pause the game to calibrate?",
                lambda: (self.abandon(), self.app.show(CalibrateScreen))),
        }
        # the approved 2-column treatment (2026-11-08); Takeback is
        # disabled until there is a move to take back (2026-11-15)
        kit.choice(self.app, "",
                   [("Home", "home"), ("New game", "new"),
                    ("End game", "end"), ("Takeback", "takeback"),
                    ("Calibration settings", "calib")],
                   lambda v: handlers[v](), columns=2,
                   disabled=() if self.game.movelist else ("takeback",))

    def _end_modal(self):
        """End game: Cancel / End, with 'See analysis?' beneath the buttons.
        The game is saved for the analysis module before the in-progress
        save is cleared."""
        m = kit.Modal(self.app, "End game")
        tk.Label(m.body, text="Abandon and clear the saved game?",
                 font=kit.F_BODY, bg=kit.CARD, fg=kit.FG, wraplength=420,
                 justify="left").pack(pady=(0, 10))
        see = tk.BooleanVar(value=False)
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack()
        kit.big_button(row, "Cancel", m.close, bg=kit.CARD_HI, fg=kit.FG,
                       width=14, height=2).pack(side="left", padx=8)

        def done():
            m.close()
            game_state.save_last()
            game_state.clear()
            self.abandon()
            self.app.show(AnalysisHomeScreen if see.get() else HomeScreen)

        kit.big_button(row, "End", done, bg=kit.RED, fg="#fff", width=14,
                       height=2).pack(side="left", padx=8)
        tk.Checkbutton(m.body, text="See analysis?", variable=see,
                       font=kit.F_BODY, bg=kit.CARD, fg=kit.FG,
                       selectcolor=kit.ACCENT, activebackground=kit.CARD,
                       activeforeground=kit.FG).pack(anchor="w", pady=(10, 0))

    def _ask_promotion(self, move):
        """The vision reports squares, not piece types: ask which piece the
        player promoted to (one tap, 2026-11-14).  The worker blocks on the
        event; the modal lives on the Tk thread.  Queen if unanswered."""
        import threading
        ev = threading.Event()
        holder = {"piece": "q"}
        self.q.put(("askpromo", lambda: self._open_promo(holder, ev)))
        ev.wait(timeout=120)
        return holder["piece"]

    def _open_promo(self, holder, ev):
        m = kit.Modal(self.app, "Promotion")
        tk.Label(m.body, text="Which piece did you promote to?",
                 font=kit.F_BODY, bg=kit.CARD, fg=kit.FG,
                 wraplength=420, justify="center").pack(pady=(0, 10))
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack()

        def pick(p):
            def _():
                holder["piece"] = p
                ev.set()
                m.close()
            return _
        for k in ("q", "r", "b", "n"):
            kit.btn(row, kit.GLYPH[k], pick(k), size="l",
                    kind="secondary",
                    width_px=110).pack(side="left", padx=6)

    def _confirm_swap(self, move):
        """The ROBOT promoted (2026-11-18): the arm only carries the pawn to
        the last rank, so the player swaps in the promoted piece by hand.
        The worker blocks here - the monitor stays paused over the swap -
        until OK; the game goes on quietly after 120 s, like the promotion
        prompt."""
        import threading
        ev = threading.Event()
        self.q.put(("promo_swap", lambda: self._open_swap(move, ev)))
        ev.wait(timeout=120)

    def _open_swap(self, move, ev):
        p = move[4].lower() if len(move) > 4 else "q"
        names = {"q": "queen", "r": "rook", "b": "bishop", "n": "knight"}
        m = kit.Modal(self.app, "Promotion")
        tk.Label(m.body, text=kit.GLYPH.get(p, "Q"), font=kit.F_TITLE,
                 bg=kit.CARD, fg=kit.ACCENT).pack(pady=(0, 4))
        tk.Label(m.body,
                 text="Swap the pawn on %s for a %s. Tap OK when done."
                      % (move[2:4], names.get(p, "queen")),
                 font=kit.F_BODY, bg=kit.CARD, fg=kit.FG,
                 wraplength=420, justify="center").pack(pady=(0, 10))
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack()
        kit.btn(row, "OK", lambda: (ev.set(), m.close()), size="m",
                kind="confirm").pack(padx=8)

    def _takeback(self):
        """Request a takeback: the worker thread does the undo + engine resync
        (engine is idle while it waits for the player's move).  Since
        2026-11-08 a modal first shows the PREVIOUS state and asks the
        player to re-set the board to it: OK proceeds, Cancel aborts."""
        m = kit.Modal(self.app, "Takeback")
        prev = self._prev_board()
        if prev is not None:
            board = kit.BoardView(m.body, px=224)
            board.pack(pady=(2, 8))
            board.draw(prev)
        tk.Label(m.body,
                 text="Re-set the board to match this position, then OK.",
                 font=kit.F_BODY, bg=kit.CARD, fg=kit.FG,
                 wraplength=420, justify="center").pack(pady=(0, 10))
        # the pieces that come BACK and where they go (2026-11-15: the
        # message no longer names the graveyard)
        for line in self._grave_notes(prev):
            tk.Label(m.body, text=line, font=kit.F_BODY, bg=kit.CARD,
                     fg=kit.FG_DIM, wraplength=420,
                     justify="center").pack()
        row = tk.Frame(m.body, bg=kit.CARD)
        row.pack()

        def ok():
            m.close()
            self._takeback_req = True
        kit.btn(row, "Cancel", m.close, size="m",
                kind="secondary").pack(side="left", padx=8)
        kit.btn(row, "OK", ok, size="m", kind="confirm").pack(side="left",
                                                              padx=8)

    def _grave_notes(self, prev):
        """Lines naming the pieces a takeback brings back from the graveyard
        and where each one sits in it."""
        out = []
        try:
            cur = self.game.board()
            grave = getattr(self.game, "grave", None)
        except Exception:
            return out
        for r in range(8):
            for c in range(8):
                if prev[r][c] == "." or cur[r][c] != ".":
                    continue
                sq = "%s%d" % (chr(97 + c), 8 - r)
                out.append("%s returns to %s"
                           % (_piece_name(prev[r][c]), sq))
        return out

    def _prev_board(self):
        """The board as it will be AFTER the takeback - the position the
        player must physically restore.  Replayed from the movelist so the
        live game state is untouched; takeback() removes the last full turn
        (up to two plies)."""
        moves = list(getattr(self.game, "movelist", []) or [])
        n = min(2, len(moves))
        if n == 0:
            return None
        try:
            import chess
            b = chess.Board()
            for u in moves[:len(moves) - n]:
                b.push_uci(u)
        except Exception:
            return None
        grid = []
        for rank in range(8, 0, -1):
            row = []
            for file in "abcdefgh":
                p = b.piece_at(chess.parse_square("%s%d" % (file, rank)))
                row.append(p.symbol() if p else ".")
            grid.append(row)
        return grid

    def abandon(self):
        self.stop_req = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--size", default="800x480")
    args = ap.parse_args()
    open(MODE_FILE, "w").write("ui")
    app = App(fullscreen=args.fullscreen, size=args.size)
    app.run()


if __name__ == "__main__":
    main()
