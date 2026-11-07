# ui_kit.py - ONE consistent look for every screen of the chess UI.
#
# Everything the app draws should come from here: the palette, buttons sized
# for fingers on a 5" screen, the Screen base class (back arrow + title),
# modal dialogs (confirm / slider / choice / text log) and the BoardView
# canvas. Screens in ui/app.py assemble these parts - do not create raw
# widgets with local styling inside screens.

import os
import tkinter as tk
from tkinter import font as tkfont

# ── palette / typography ─────────────────────────────────────────────────────
BG        = "#0c0a09"   # warm black - the dark room (theme: cherry & gold)
CARD      = "#2b1512"   # panels, buttons (cherry-tinted)
CARD_HI   = "#3c1c17"   # pressed/hover
FG        = "#f4e9d8"   # bone - primary text
FG_DIM    = "#c0a088"   # dim - secondary text
ACCENT    = "#e6b25e"   # gold - primary action (the board's light side)
ACCENT_HI = "#c9933f"   # gold pressed
GREEN     = "#2f8f9e"   # TEAL - confirm: gold's axis partner, never
                        # red-vs-green (red-green CVD, 2026-11-07)
RED       = "#b34733"   # brick - danger FILL (use ALERT for red TEXT)
ALERT     = "#e07a5f"   # red that reads on dark - text/borders only
EDGE      = "#b8863f"   # hairline border on secondary/danger buttons
SQUARE_L  = "#e7c182"   # gold board - light squares
SQUARE_D  = "#cb8f42"   # gold board - dark squares
BOARD_BG  = "#0a0806"   # the black barrier around the board
PIECE_L   = "#f6efe2"   # near-white pine - white pieces
PIECE_D   = "#3f2a1c"   # deep walnut - black pieces
TABLE     = "#7a241c"   # the cherry table

F_TITLE = ("Atkinson Hyperlegible", 20, "bold")
F_BODY  = ("Atkinson Hyperlegible", 15)
F_SMALL = ("Atkinson Hyperlegible", 11)
F_BTN   = ("Atkinson Hyperlegible", 16, "bold")
F_PIECE = ("DejaVu Sans", 22)     # chess glyphs: Atkinson has none
F_MONO  = ("JetBrains Mono", 14)  # numeric readouts
F_HERO  = ("Cinzel", 56, "bold")  # the chessbot wordmark

# THE instruction text width: every screen's instructions wrap at this width.
# It is a property of the component (instr_label) - not of each call site -
# so no screen can ship a string that runs off the right edge again.
INSTR_WRAP = 760

GLYPH = {"k": "\u265a", "q": "\u265b", "r": "\u265c", "b": "\u265d",
         "n": "\u265e", "p": "\u265f"}


def _ensure_fonts():
    """Install the bundled TTFs into the USER font dir (no root needed) and
    refresh the fontconfig cache so Tk resolves the families by name.
    Runs at import, before the Tk root exists.  Silent on failure - the
    families then fall back to whatever the system has."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "assets", "fonts")
    if not os.path.isdir(src):
        return
    try:
        import shutil
        import subprocess
        dst = os.path.join(os.path.expanduser("~"), ".local", "share",
                           "fonts", "chessbot")
        os.makedirs(dst, exist_ok=True)
        fresh = False
        for fn in sorted(os.listdir(src)):
            if not fn.lower().endswith(".ttf"):
                continue
            tgt = os.path.join(dst, fn)
            if not os.path.exists(tgt):
                shutil.copy(os.path.join(src, fn), tgt)
                fresh = True
        if fresh:
            subprocess.run(["fc-cache", "-f", dst],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    except Exception:
        pass


_ensure_fonts()


def style_root(root):
    root.configure(bg=BG)
    root.tk_setPalette(background=BG, foreground=FG,
                       activeBackground=CARD_HI, activeForeground=FG)


def big_button(parent, text, cmd, bg=ACCENT, fg="#1b1e24", font=F_BTN,
               height=2, width=24, disabled=False, wraplength=None,
               justify="center"):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=_PRESSED.get(bg, bg),
                  activeforeground=fg, font=font,
                  relief="flat", bd=0,
                  highlightthickness=(1 if bg not in (ACCENT, GREEN) else 0),
                  highlightbackground=(ALERT if bg == RED else EDGE),
                  height=height, width=width, wraplength=wraplength,
                  justify=justify)
    if disabled:
        b.configure(state="disabled", bg=_DIS_BG, fg=_DIS_FG,
                    highlightbackground=_DIS_EDGE)
    return b


# pressed fills (one luminance step darker - never a hue-only change) and
# the uniform disabled treatment (2026-11-07)
_PRESSED = {ACCENT: ACCENT_HI, GREEN: "#24707c", CARD: "#5a2c22",
            CARD_HI: "#5a2c22", RED: "#8f3325"}
_DIS_BG, _DIS_FG, _DIS_EDGE = "#2e2420", "#8a7466", "#4a3c2c"

# the sizing ladder: S/M/L/XL by HEIGHT (px) - the touch target.  Width
# adapts to the label; stacked buttons of one size share a width.
LADDER = {"s": (96, 48, 13), "m": (140, 56, 15),
          "l": (200, 64, 17), "xl": (260, 80, 20)}


def btn(parent, text, cmd, size="m", kind="secondary", square=False,
        disabled=False, wraplength=None):
    """Ladder button (2026-11-07): exact PIXEL sizing - Tk width is in
    text units, so it is converted through the font metrics and the button
    keeps its size whatever font wins.  kind: primary / confirm /
    secondary / danger."""
    w_px, h_px, fs = LADDER[size]
    if square:
        w_px = h_px
    font = (F_BTN[0], fs, "bold")
    f = tkfont.Font(font=font)
    cw = max(1, f.measure("0"))
    lh = max(1, f.metrics("linespace"))
    fills = {"primary": (ACCENT, "#1b1e24", None),
             "confirm": (GREEN, "#1b1e24", None),
             "secondary": (CARD_HI, FG, EDGE),
             "danger": (RED, FG, ALERT)}
    bg, fg, bd = fills.get(kind, fills["secondary"])
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=_PRESSED.get(bg, bg),
                  activeforeground=fg, font=font, relief="flat", bd=0,
                  highlightthickness=(1 if bd else 0),
                  highlightbackground=bd or bg,
                  height=max(1, int(h_px / lh)),
                  width=max(1, int(w_px / cw)),
                  wraplength=wraplength, justify="center")
    if disabled:
        b.configure(state="disabled", bg=_DIS_BG, fg=_DIS_FG,
                    highlightbackground=_DIS_EDGE)
    return b


def btn_stack(parent, items, size="m"):
    """A vertical stack of ladder buttons (2026-11-07).  items =
    [(text, cmd, kind)] or [(text, cmd, kind, size)].  The stack rule:
    stacked buttons share ONE width - fill="x" stretches every button to
    the column, so even mixed sizes line up exactly.  Even 8px gaps."""
    col = tk.Frame(parent, bg=BG)
    out = []
    n = len(items)
    for i, it in enumerate(items):
        text, cmd, kind = it[0], it[1], it[2]
        sz = it[3] if len(it) > 3 else size
        b = btn(col, text, cmd, size=sz, kind=kind)
        b.pack(fill="x", pady=(0, 8 if i < n - 1 else 0))
        out.append(b)
    return col, out


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


def glyph_photo(name, size=None):
    """PIL-drawn icon from ui/assets/<name>.png ('pause', 'play', ...).
    size= rescales the asset smoothly (PIL) for tall buttons."""
    key = (name, size)
    if key in _GLYPH_PHOTOS:
        return _GLYPH_PHOTOS[key] or None
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ui", "assets", name + ".png")
    photo = None
    try:
        if size:
            from PIL import Image, ImageTk
            img = Image.open(path)
            w, h = img.size
            img = img.resize((max(1, round(size * w / h)), size),
                             Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        else:
            photo = tk.PhotoImage(file=path)
    except Exception:
        try:
            photo = tk.PhotoImage(file=path)
        except tk.TclError:
            photo = None
    _GLYPH_PHOTOS[key] = photo or False
    return photo


# Black piece icons with a white outline (2026-11-06): the filled black
# glyphs are nearly invisible on the dark text background.  PIL renders the
# glyph with a stroke - the same trick the board uses for its pieces.
_PIECE_PHOTOS = {}


def piece_photo(glyph, size=22):
    key = (glyph, size)
    if key in _PIECE_PHOTOS:
        return _PIECE_PHOTOS[key] or None
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageTk
        font = None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/TTF/DejaVuSans.ttf"):
            if os.path.exists(path):
                font = ImageFont.truetype(path, size)
                break
        if font is None:
            font = ImageFont.load_default()
        img = Image.new("RGBA", (size + 10, size + 12), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((5, 1), glyph, font=font, fill=(63, 42, 28, 255),
               stroke_width=max(1, size // 12),
               stroke_fill=(244, 233, 216, 255))
        photo = ImageTk.PhotoImage(img)
    except Exception:
        photo = None
    _PIECE_PHOTOS[key] = photo or False
    return photo


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
    """A CHECKBOX: the drawn box + the label (2026-10-24). Not a button -
    a flat label the player taps. Tk's own indicator never grows with the
    font, so the box is the PIL-drawn PNG, swapped on toggle."""
    img = _check_photo(bool(variable.get()))
    lbl = tk.Label(parent, text=text, image=img, compound="left",
                   bg=bg, fg=fg, font=font or F_BODY,
                   anchor="w", padx=6, pady=6, cursor="hand2")
    lbl.image = img

    def toggle(_event=None):
        variable.set(not bool(variable.get()))
        new = _check_photo(bool(variable.get()))
        if new is not None:
            lbl.configure(image=new)
            lbl.image = new
        if command:
            command()
    lbl.bind("<Button-1>", toggle)
    return lbl


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

def draw_checker(c, size=80, light="#1d1a17", dark=BG):
    """The app's signature checker (home and modal backgrounds, 2026-11-08):
    soft grey squares on the warm black - texture at a glance, never loud."""
    try:
        c.delete("checker")
        w, h = c.winfo_width(), c.winfo_height()
        for r in range(h // size + 1):
            for col in range(w // size + 1):
                if (r + col) % 2 == 0:
                    c.create_rectangle(col * size, r * size,
                                       col * size + size, r * size + size,
                                       fill=light, outline="", tags="checker")
    except tk.TclError:
        pass


class Modal:
    """Checker backdrop + centred card over the whole app window.  The
    backdrop carries the home's checker (2026-11-08) so every modal screen
    has the app's texture around its card."""

    def __init__(self, app, title):
        self.app = app
        self.overlay = tk.Canvas(app.root, bg=BG, highlightthickness=0)
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.bind("<Configure>",
                          lambda _e: draw_checker(self.overlay))
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
                         bg=BOARD_BG, highlightthickness=0, bd=0, **kw)

    def resize(self, px):
        """Grow/shrink in place (BoardCols refits the board to its column;
        bindings survive because the widget is not recreated)."""
        px = (max(64, px) // 8) * 8
        self.px = px
        self.sq = px // 8
        self.configure(width=px, height=px)

    def draw(self, board, last_move=None, arrow=None, arrow_fg=None):
        self.delete("all")
        for r in range(8):
            for c in range(8):
                x0, y0 = c * self.sq, r * self.sq
                light = (r + c) % 2 == 0
                self.create_rectangle(x0, y0, x0 + self.sq, y0 + self.sq,
                                      fill=SQUARE_L if light else SQUARE_D,
                                      outline=BOARD_BG)
                piece = board[r][c]
                if piece != ".":
                    black = piece.islower()
                    if black:
                        self.create_text(
                            x0 + self.sq // 2, y0 + int(self.sq * 0.56),
                            text=GLYPH.get(piece.lower(), "?"),
                            font=F_PIECE, fill=PIECE_D)
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


# ── two-column board layout + scaling text (2026-11-05) ─────────────────
#
# The analysis screens' layout (big board in the left column, controls and
# text right) is now the standard for every board screen: the board fills
# the left column at MAX HEIGHT, the right column holds the pressable
# controls first and the text block after them, and the text scales down
# so nothing runs outside the window.

def avail_h(widget):
    """Pixels left for widget vertically: its master's height minus the
    height of the master's other (mapped) children."""
    try:
        m = widget.master
        h = m.winfo_height()
        if h < 40:
            return 0
        used = 0
        for c in m.winfo_children():
            if c is not widget and c.winfo_ismapped():
                used += c.winfo_height() + 6
        return max(0, h - used - 6)
    except tk.TclError:
        return 0


def _wrap_lines(text, width, font):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = word if not cur else cur + " " + word
            if not cur or font.measure(trial) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


class FitLabel(tk.Label):
    """Instruction text that scales itself (2026-11-05): wrapped at the
    label's width and shrunk from its base font until the block fits the
    space left under the controls - the text must never run outside the
    window.  Drop-in for instr_label; set text with configure(text=...) or
    set().  <Configure> re-fits."""

    MIN_SIZE = 9

    def __init__(self, parent, text="", font=None, **kw):
        self._full = text
        self._font = tuple(font or F_BODY)
        kw.setdefault("bg", BG)
        kw.setdefault("fg", FG)
        kw.setdefault("justify", "left")
        kw.setdefault("anchor", "w")
        # pre-refit wrap: an unwrapped Label requests the whole text on one
        # line and would blow the column's width request out
        kw.setdefault("wraplength", 420)
        super().__init__(parent, text=text, font=self._font, **kw)
        self.bind("<Configure>", lambda *_: self._refit(), add="+")
        parent.bind("<Configure>", lambda *_: self._refit(), add="+")
        self.after_idle(self._refit)

    def set(self, text, color=None):
        self._full = text
        if color is not None:
            self.configure(fg=color)
        self._refit()

    def configure(self, cnf=None, **kw):
        changed = False
        if isinstance(cnf, dict) and "text" in cnf:
            self._full = cnf["text"]
            changed = True
        if "text" in kw:
            self._full = kw["text"]
            changed = True
        super().configure(cnf, **kw)
        if changed:
            self.after_idle(self._refit)

    config = configure

    def _refit(self):
        if not self.winfo_exists():
            return
        try:
            text = self._full or ""
            width = max(60, self.winfo_width() - 6)
            avail = avail_h(self) or 120
            fam = self._font[0]
            bold = "bold" in self._font[2:]
            base = int(self._font[1])
            pick = None
            for sz in range(base, self.MIN_SIZE - 1, -1):
                f = tkfont.Font(family=fam, size=sz,
                                weight="bold" if bold else "normal")
                need = (len(_wrap_lines(text, width, f))
                        * f.metrics("linespace"))
                if need <= avail:
                    pick = sz
                    break
            if pick is None:
                pick = self.MIN_SIZE
            self.configure(font=(fam, pick) + (("bold",) if bold else ()),
                           wraplength=width)
        except tk.TclError:
            pass


class BoardCols:
    """Two-column layout for board screens (2026-11-05): the board fills
    the LEFT column at max height (re-fitted whenever the layout changes),
    every other element goes in .right - pressable controls first, then
    the text block.  Usage:

        cols = kit.BoardCols(self.body)
        self.board = kit.BoardView(cols.board_col, px=256)
        cols.set_board(self.board, redraw=self._draw)
        ... controls packed into cols.right ...
    """

    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg=BG)
        self.frame.pack(fill="both", expand=True)
        self.board_col = tk.Frame(self.frame, bg=BG)
        self.board_col.pack(side="left", fill="y")
        self.right = tk.Frame(self.frame, bg=BG)
        self.right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self._board = None
        self._redraw = None

    def set_board(self, board, redraw=None):
        """board must be a BoardView whose master is board_col (or a frame
        inside it), already packed by the caller."""
        self._board = board
        self._redraw = redraw
        if board.master is self.board_col:
            board.pack()
        self.board_col.bind("<Configure>", lambda *_: self._fit(), add="+")
        self.frame.after_idle(self._fit)

    def _fit(self, _e=None):
        b = self._board
        if b is None or not b.winfo_exists():
            return
        h = self.board_col.winfo_height()
        if h < 120:                      # not realized yet
            return
        avail = 780 - 12 - self.right.winfo_reqwidth()
        px = (max(192, min(h - 6, avail)) // 8) * 8
        if px == b.px:
            return
        b.resize(px)
        if self._redraw is not None:
            self._redraw()


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
