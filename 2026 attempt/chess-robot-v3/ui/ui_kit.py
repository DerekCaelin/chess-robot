# ui_kit.py - ONE consistent look for every screen of the chess UI.
#
# Everything the app draws should come from here: the palette, buttons sized
# for fingers on a 5" screen, the Screen base class (back arrow + title),
# modal dialogs (confirm / slider / choice / text log) and the BoardView
# canvas. Screens in ui/app.py assemble these parts - do not create raw
# widgets with local styling inside screens.

import os
import tkinter as tk

# ── palette / typography ─────────────────────────────────────────────────────
BG        = "#232730"   # app background (slate)
CARD      = "#2e3440"   # panels, buttons
CARD_HI   = "#3b4252"   # pressed/hover
FG        = "#eceef4"
FG_DIM    = "#9aa3b5"
ACCENT    = "#d08b3e"   # primary action (warm wood)
ACCENT_HI = "#b3752e"
GREEN     = "#4f9e6b"
RED       = "#c25a52"
SQUARE_L  = "#e8d9b8"
SQUARE_D  = "#7d6a4e"

F_TITLE = ("DejaVu Sans", 20, "bold")
F_BODY  = ("DejaVu Sans", 15)
F_SMALL = ("DejaVu Sans", 11)
F_BTN   = ("DejaVu Sans", 16, "bold")
F_PIECE = ("DejaVu Sans", 22)

# THE instruction text width: every screen's instructions wrap at this width.
# It is a property of the component (instr_label) - not of each call site -
# so no screen can ship a string that runs off the right edge again.
INSTR_WRAP = 760

GLYPH = {"k": "\u265a", "q": "\u265b", "r": "\u265c", "b": "\u265d",
         "n": "\u265e", "p": "\u265f"}


def style_root(root):
    root.configure(bg=BG)
    root.tk_setPalette(background=BG, foreground=FG,
                       activeBackground=CARD_HI, activeForeground=FG)


def big_button(parent, text, cmd, bg=ACCENT, fg="#1b1e24", font=F_BTN,
               height=2, width=24, disabled=False, wraplength=None,
               justify="center"):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=bg, activeforeground=fg, font=font,
                  relief="flat", bd=0, highlightthickness=0,
                  height=height, width=width, wraplength=wraplength,
                  justify=justify)
    if disabled:
        b.configure(state="disabled", bg=CARD, fg=FG_DIM)
    return b


def instr_label(parent, font=None, **kw):
    """THE instruction text component: wraps at INSTR_WRAP by construction,
    left aligned, standard colours. Every screen's instructions come from
    here (2026-10-21)."""
    return tk.Label(parent, font=font or F_BODY, bg=BG, fg=FG,
                    wraplength=INSTR_WRAP, justify="left", anchor="w", **kw)


# The circled-i glyph (U+24D8) is missing from DejaVu Sans on the Pi, so the
# info buttons rendered as empty rectangles.  Ship a PIL-drawn PNG instead.
# Same for the pause glyph (U+23F8): play/pause transport icons are PNGs.
_INFO_PHOTOS = {}
_GLYPH_PHOTOS = {}


def glyph_photo(name):
    """PIL-drawn icon from ui/assets/<name>.png ('pause', 'play', ...)."""
    if name not in _GLYPH_PHOTOS:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "assets", name + ".png")
        try:
            _GLYPH_PHOTOS[name] = tk.PhotoImage(file=path)
        except tk.TclError:
            _GLYPH_PHOTOS[name] = False
    return _GLYPH_PHOTOS[name] or None


# Touch-sized checkboxes (2026-10-24): Tk's checkbutton INDICATOR does not
# grow with the font, so the box is a PIL-drawn PNG (the info/pause pattern)
# swapped on toggle.
_CHECK_PHOTOS = {}


def _check_photo(on):
    name = "check_on" if on else "check_off"
    if name not in _CHECK_PHOTOS:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "assets", name + ".png")
        try:
            _CHECK_PHOTOS[name] = tk.PhotoImage(file=path)
        except tk.TclError:
            _CHECK_PHOTOS[name] = False
    return _CHECK_PHOTOS[name] or None


def checkbox(parent, text, variable, command=None, font=None, bg=BG,
             fg=FG):
    """A finger-sized toggle: the drawn box left of the label."""
    img = _check_photo(bool(variable.get()))
    b = tk.Button(parent, text=text, image=img, compound="left",
                  bg=bg, fg=fg, activebackground=CARD_HI,
                  activeforeground=fg, font=font or F_BODY, relief="flat",
                  bd=0, highlightthickness=0, anchor="w", padx=6, pady=8)
    b.image = img

    def toggle():
        variable.set(not bool(variable.get()))
        new = _check_photo(bool(variable.get()))
        if new is not None:
            b.configure(image=new)
            b.image = new
        if command:
            command()
    b.configure(command=toggle)
    return b


def _info_photo(small=False):
    name = "info_small.png" if small else "info.png"
    if name not in _INFO_PHOTOS:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "assets", name)
        try:
            _INFO_PHOTOS[name] = tk.PhotoImage(file=path)
        except tk.TclError:
            _INFO_PHOTOS[name] = False
    return _INFO_PHOTOS[name] or None


def info_button(parent, command, small=False, bg=CARD):
    """Info icon button (PIL-drawn PNG); falls back to a '?' glyph if the
    asset is missing."""
    icon = _info_photo(small)
    if icon is not None:
        b = tk.Button(parent, image=icon, command=command, bg=bg,
                      activebackground=CARD_HI, relief="flat", bd=0,
                      highlightthickness=0, padx=10 if not small else 6,
                      pady=10 if not small else 6)
        b.image = icon          # keep a reference alive
        return b
    return big_button(parent, "?", command, bg=CARD, fg=FG,
                      width=3, height=2, font=F_BTN)


# ── modal dialogs ────────────────────────────────────────────────────────────

class Modal:
    """Dimming overlay + centred card over the whole app window."""

    def __init__(self, app, title):
        self.app = app
        self.overlay = tk.Frame(app.root, bg="#14161c")
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        card = tk.Frame(self.overlay, bg=CARD, bd=0,
                        highlightbackground=ACCENT, highlightthickness=2)
        card.place(relx=0.5, rely=0.5, anchor="center")
        if title:
            tk.Label(card, text=title, font=F_TITLE, bg=CARD, fg=FG).pack(
                padx=20, pady=(14, 6))
        self.body = tk.Frame(card, bg=CARD)
        self.body.pack(padx=20, pady=(0, 14))
        self.card = card

    def close(self):
        self.overlay.destroy()


def confirm(app, title, message, on_yes, on_no=None, yes_text="Yes",
            no_text="Cancel", danger=False):
    m = Modal(app, title)
    tk.Label(m.body, text=message, font=F_BODY, bg=CARD, fg=FG,
             wraplength=520, justify="left").pack(pady=4)
    row = tk.Frame(m.body, bg=CARD)
    row.pack(pady=(12, 0))

    def both(fn):
        m.close()
        if fn:
            fn()
    big_button(row, no_text, lambda: both(on_no), bg=CARD_HI, fg=FG,
               width=14).pack(side="left", padx=8)
    big_button(row, yes_text, lambda: both(on_yes),
               bg=RED if danger else GREEN, width=14).pack(side="left", padx=8)
    return m


def slider(app, title, lo, hi, step, value, on_change, fmt=None,
           unit="", confirm_msg=None, extra=None):
    """Value editor: big +/- keys and a wide scale.
    The scale AND the +/- keys both move in exact `step` increments (dragging
    snaps to the same grid as the buttons), so you never land on an odd value
    like 1.0201. Shows the original and moving value; Cancel reverts, Done
    keeps. When confirm_msg is set the change is NOT applied live - Done first
    asks 'Are you sure?' (for destructive edits like the arm constants).
    extra(value) adds a suffix to the shown number (the Elo of a level)."""
    import math as _m

    def _dec(s):                       # decimals implied by the step size
        return max(0, int(_m.ceil(-_m.log10(s) - 1e-9))) if s > 0 else 0
    ndec = _dec(step)
    if fmt is None:
        fmt = "%d" if ndec == 0 else "%." + str(ndec) + "f"

    def snap(v):                       # snap to lo + k*step, clamp to range
        k = round((v - lo) / step)
        return min(hi, max(lo, lo + k * step))

    orig = snap(float(value))
    m = Modal(app, title)
    val = tk.DoubleVar(value=orig)
    head = tk.Label(m.body, font=F_TITLE, bg=CARD, fg=ACCENT)
    head.pack()
    tk.Label(m.body, text="was " + fmt % orig + unit, font=F_SMALL,
             bg=CARD, fg=FG_DIM).pack()
    sc = tk.Scale(m.body, from_=lo, to=hi, resolution=step, orient="horizontal",
                  variable=val, showvalue=0, length=420, width=28,
                  sliderlength=64, bg=CARD, troughcolor=CARD_HI,
                  highlightthickness=0, border=0, fg=FG, font=F_SMALL)
    sc.pack(pady=8)

    def render():
        v = val.get()
        head.configure(text=fmt % v + (extra(v) if extra else "") + unit)

    def push(*_ignore):        # Tk variable traces hand us (name, index, op)
        render()
        if confirm_msg is None:
            on_change(round(snap(val.get()), 6))
    val.trace_add("write", push)
    render()
    row = tk.Frame(m.body, bg=CARD)
    row.pack(pady=2)

    def nudge(d):
        val.set(snap(round(snap(val.get()) + d, 6)))
    big_button(row, "\u2212", lambda: nudge(-step), bg=CARD_HI,
               width=6).pack(side="left", padx=6)
    big_button(row, "+", lambda: nudge(step), bg=CARD_HI,
               width=6).pack(side="left", padx=6)
    btns = tk.Frame(m.body, bg=CARD)
    btns.pack(pady=(12, 0))

    def cancel():
        val.set(orig)          # write-back reverts the live-apply path
        m.close()

    def done():
        m.close()
        if confirm_msg is not None:
            confirm(app, "Are you sure?", "",
                    lambda: on_change(round(snap(val.get()), 6)),
                    danger=True, yes_text="Yes")
    big_button(btns, "Cancel (revert)", cancel, bg=CARD, fg=FG_DIM,
               width=16, height=2, font=F_BODY).pack(side="left", padx=6)
    big_button(btns, "Done", done, bg=GREEN, width=14, height=2,
               font=F_BODY).pack(side="left", padx=6)
    return m


def choice(app, title, options, on_pick, current=None):
    """options: list of (label, value). on_pick(value) fires and closes."""
    m = Modal(app, title)
    for label, value in options:
        bg = ACCENT if value == current else CARD_HI
        fg = "#1b1e24" if value == current else FG
        big_button(m.body, label,
                   lambda v=value: (m.close(), on_pick(v)),
                   bg=bg, fg=fg, width=22, height=1,
                   font=F_BODY).pack(fill="x", pady=4, padx=10)
    big_button(m.body, "Cancel", m.close, bg=CARD, fg=FG_DIM, width=22,
               height=1, font=F_BODY).pack(pady=(6, 0))
    return m


def textlog(app, title):
    """Returns (modal, append_fn) - a live scrolling log (for tool runs)."""
    m = Modal(app, title)
    txt = tk.Text(m.body, width=72, height=16, bg="#181b21", fg=FG,
                  insertwidth=0, relief="flat", font=("DejaVu Sans Mono", 9),
                  wrap="word")
    txt.pack()
    txt.configure(state="disabled")

    def append(line):
        txt.configure(state="normal")
        txt.insert("end", line + "\n")
        txt.see("end")
        txt.configure(state="disabled")
    return m, append


# ── screen base ──────────────────────────────────────────────────────────────

class Screen(tk.Frame):
    """A page: header row with optional back arrow + title, plus a body."""

    BACK = True   # subclasses set False for the home screen

    def __init__(self, master=None, app=None, title="", info=None, **kw):
        super().__init__(master or app.root, bg=BG, **kw)
        self.app = app
        self.title_text = title
        # Title bar is deliberately as tall as a primary button (height=2) so
        # the back arrow is a finger-sized target; the info icon, when a
        # screen supplies one, mirrors it on the right.
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", side="top")
        if self.BACK:
            big_button(head, "\u25C0", self.request_back, bg=CARD, fg=FG,
                       width=4, height=2, font=F_BTN).pack(side="left",
                                                           padx=8, pady=6)
        if info:
            info_button(head, info).pack(side="right", padx=8, pady=6)
        if title:
            tk.Label(head, text=title, font=F_TITLE, bg=BG, fg=FG).pack(
                side="left", padx=8, pady=10)
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=10, pady=4)

    def on_show(self):
        """Called by the router right after the screen becomes visible."""

    def begin_close(self):
        """True exactly once per close. A second Cancel/Save tap while the
        park-and-go-back is still in flight must be IGNORED: it used to run
        park() twice and pop two screens (2026-10-21)."""
        if getattr(self, "_closing", False):
            return False
        self._closing = True
        return True

    def request_back(self):
        """Back-arrow behaviour; calibration screens override to Cancel."""
        self.app.back()


# ── board rendering ──────────────────────────────────────────────────────────

class BoardView(tk.Canvas):
    """8x8 board from ChessBoard's getBoard() layout:
    row 0 = rank 8, col 0 = a-file; '.' empty, case = colour."""

    def __init__(self, master, px=256, **kw):
        # the canvas MUST request the real board size - left at the Tk
        # default (~450x338) a 430px board cropped to ~4 files / 4 ranks
        self.px = (px // 8) * 8
        self.sq = self.px // 8
        super().__init__(master, width=self.px, height=self.px,
                         bg=BG, highlightthickness=0, bd=0, **kw)

    def draw(self, board, last_move=None, arrow=None, arrow_fg=None):
        self.delete("all")
        for r in range(8):
            for c in range(8):
                x0, y0 = c * self.sq, r * self.sq
                light = (r + c) % 2 == 0
                self.create_rectangle(x0, y0, x0 + self.sq, y0 + self.sq,
                                      fill=SQUARE_L if light else SQUARE_D,
                                      outline="#5a4c37")
                piece = board[r][c]
                if piece != ".":
                    black = piece.islower()
                    if black:
                        self.create_text(
                            x0 + self.sq // 2, y0 + int(self.sq * 0.56),
                            text=GLYPH.get(piece.lower(), "?"),
                            font=F_PIECE, fill="#16181d")
                    else:
                        # dark outline so light pieces pop on light squares.
                        # Canvas-text stroke options only exist in Tk 9 -
                        # on Tk 8.6 they raise TclError mid-draw (partial
                        # board, 2026-09-18). Fake it: dark glyph at the 8
                        # one-pixel offsets, light glyph on top.
                        cx = x0 + self.sq // 2
                        cy = y0 + int(self.sq * 0.56)
                        gl = GLYPH.get(piece.lower(), "?")
                        for dx in (-1, 0, 1):
                            for dy in (-1, 0, 1):
                                if dx or dy:
                                    self.create_text(cx + dx, cy + dy,
                                                     text=gl, font=F_PIECE,
                                                     fill="#16181d")
                        self.create_text(cx, cy, text=gl, font=F_PIECE,
                                         fill="#f6f2e8")
        if last_move:
            for (fr, fc, tr, tc) in [last_move]:
                for (rr, cc) in ((fr, fc), (tr, tc)):
                    self.create_rectangle(cc * self.sq + 2, rr * self.sq + 2,
                                          (cc + 1) * self.sq - 2,
                                          (rr + 1) * self.sq - 2,
                                          outline=ACCENT, width=3)
        # the recommended move as a player-coloured arrow (2026-10-23): a
        # pawn to e6 is an arrow from e7 to e6. A dark outline keeps a black
        # arrow readable on dark squares.
        if arrow:
            (fr, fc, tr, tc) = arrow
            x0 = fc * self.sq + self.sq // 2
            y0 = fr * self.sq + self.sq // 2
            x1 = tc * self.sq + self.sq // 2
            y1 = tr * self.sq + self.sq // 2
            fg = arrow_fg or "#f6f2e8"
            w = max(3, self.sq // 7)
            shape = (self.sq // 3, self.sq // 2, self.sq // 5)
            self.create_line(x0, y0, x1, y1, fill="#16181d", width=w + 4,
                             arrow=tk.LAST,
                             arrowshape=(shape[0] + 5, shape[1] + 5,
                                         shape[2] + 5))
            self.create_line(x0, y0, x1, y1, fill=fg, width=w,
                             arrow=tk.LAST, arrowshape=shape)


class EvalChart(tk.Canvas):
    """The engine's evaluation of positional strength at every move: white
    above the middle line, black below. The PLAYER's stars are white; the
    game's three most consequential moves are orange; the current move has
    a marker. The chart grows as the analysis advances (2026-10-24)."""

    LINE = "#c8d0dc"
    ZERO = "#3b4252"
    STAR_PLAYER = "#f6f2e8"
    STAR_TURN = "#e08b3e"

    def __init__(self, master, w=380, h=132, **kw):
        super().__init__(master, width=w, height=h, bg="#181b21",
                         highlightthickness=0, **kw)
        self.w, self.h = w, h

    def _star(self, x, y, col):
        self.create_text(x, y, text="\u2605", fill=col,
                         font=("DejaVu Sans", 15))

    def update_moves(self, moves, stars=(), turning=(), current=None,
                     total=None, player="white"):
        self.delete("all")
        mid = self.h // 2
        n = max(2, total or len(moves))     # the game length is KNOWN: the
        span = self.w / float(n)            # x scale never re-computes
        mine = "#e8e4da" if player == "white" else "#20242c"
        theirs = "#20242c" if player == "white" else "#e8e4da"

        def x(ply):
            return (ply - 0.5) * span

        def y(cp):
            v = max(-1200.0, min(1200.0, float(cp or 0)))
            return mid - int((mid - 8) * v / 1200.0)
        pts = [(x(m.get("ply", i + 1)), y(m.get("eval_cp", 0)))
               for i, m in enumerate(moves)]
        if pts:
            curve = [c for p in pts for c in p]
            # the territory split (2026-10-24): the PLAYER's colour fills
            # everything under the line, the opponent's everything over it
            self.create_polygon(curve + [pts[-1][0], self.h,
                                         pts[0][0], self.h],
                                fill=mine, outline="")
            self.create_polygon(curve + [pts[-1][0], 0, pts[0][0], 0],
                                fill=theirs, outline="")
            self.create_line(*curve, fill=self.LINE, width=3, smooth=True)
        self.create_line(0, mid, self.w, mid, fill=self.ZERO, width=2)
        starset, turnset = set(stars), set(turning)
        for m in moves:
            ply = m.get("ply", 0)
            px, py = x(ply), y(m.get("eval_cp", 0))
            if ply in turnset:
                self._star(px, py, self.STAR_TURN)
            if ply in starset:
                self._star(px, py, self.STAR_PLAYER)
        if current:
            cx = x(current)
            self.create_line(cx, 2, cx, self.h - 2, fill=ACCENT, width=2)
