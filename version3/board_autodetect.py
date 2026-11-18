# Copyright 2026 Derek Caelin - written by a coding agent under his direction.
# Licensing and credits: see README.md, "Credits & provenance".
# board_autodetect.py
#
# Automatic chessboard detection for the chess robot.
#
# Public API:
#   detect_board(img) -> {"A1":(x,y), "H1":(x,y), "A8":(x,y), "H8":(x,y)}
#                        coordinates normalised to [0,1] relative to the image,
#                        or None if the board could not be detected.
#
# Detection cascade (first success wins):
#   1. findChessboardCorners  — the empty board is a clean 8x8 checkerboard,
#      giving sub-pixel internal corners.
#   2. Middle-band 7x3        — the starting position leaves ranks 3-6 empty,
#      a clean 8x4 checkerboard strip.
#   3. Partial windows        — piece shadows and glare frequently break the
#      full band, so we accept any clean sub-window (>=3x3 internal corners)
#      found under contrast-normalised pre-processing, resolve where it sits
#      on the board lattice by scoring full-board square parity, and anchor a
#      second window on the same lattice to remove the sliding ambiguity.
#
# There is deliberately NO outline/Hough fallback: those experiments returned
# garbage corners and produced the calibration "jumps"; an honest None is
# safer, and the live tool holds its last good detection meanwhile.
#
# Orientation is recovered automatically from the piece colours:
#   - the board edge holding the light (white) pieces is rank 1,
#   - on that edge the dark corner is a1 (h1 is light).
# When the board is empty the piece signal is absent, so the result has a
# possible 180-degree ambiguity (a1<->h8).  That is expected; the calibration
# flow asks the user for a single A1 click in that case.

import time
import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _order_clockwise(pts):
    """Return the 4 points ordered counter-clockwise around their centroid."""
    pts = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
    centroid = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - centroid[1], pts[:, 0] - centroid[0])
    return pts[np.argsort(angles)]


def _outer_from_homography(internal, ideal_x, ideal_y):
    """Fit a perspective homography from the internal corner grid to its known
    grid-line coordinates, then map the four outer corners through it.

    internal : (rows*cols, 2) row-major internal corners
    ideal_x  : grid-line x-coordinates of the internal corners (columns)
    ideal_y  : grid-line y-coordinates of the internal corners (rows)

    The board's grid lines run 0..8, so the outer corners of the playing area
    are at (0,0), (8,0), (8,8), (0,8).  Fitting across every internal corner
    (instead of extrapolating from the four extremes) averages out per-corner
    noise — which matters most for the dark squares, whose outer edges have low
    contrast against the table.
    """
    rows = len(ideal_y)
    cols = len(ideal_x)
    grid = np.asarray(internal, dtype=np.float32).reshape(rows, cols, 2)
    dst = grid.reshape(-1, 2)
    src = np.float32([[x, y] for y in ideal_y for x in ideal_x])
    H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        return None
    outer_ideal = np.float32([[0, 0], [8, 0], [8, 8], [0, 8]]).reshape(-1, 1, 2)
    outers = cv2.perspectiveTransform(outer_ideal, H).reshape(-1, 2)
    return _order_clockwise(outers)


def _find_internal(img, pattern):
    """findChessboardCorners for `pattern`, refined with cornerSubPix.

    findChessboardCorners often fails on large squares at full resolution (its
    adaptive-threshold block is a fixed size), so we retry on progressively
    downscaled images and map the resulting corners back to full resolution,
    where they are refined to sub-pixel accuracy.
    """
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape
    for scale in (1.0, 0.75, 0.5, 0.35):
        g = grey if scale >= 1.0 else cv2.resize(
            grey, (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA)
        found, corners = cv2.findChessboardCorners(
            g, pattern,
            cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            continue
        corners = corners.reshape(-1, 2)
        if scale < 1.0:
            corners = corners / scale
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(grey, corners, (11, 11), (-1, -1), criteria)
        return corners.astype(np.float32)
    return None


def _detect_chessboard(img):
    """Full empty board: 7x7 internal corners -> 4 outer corners."""
    internal = _find_internal(img, (7, 7))
    if internal is None:
        return None
    return _outer_from_homography(internal, list(range(1, 8)), list(range(1, 8)))


def _detect_middle_band(img):
    """Pieces on ranks 1-2 and 7-8 leave the middle four ranks empty, so an
    8x4 sub-board is still a clean checkerboard: 7x3 internal corners.
    The empty band sits on grid lines y=3,4,5."""
    internal = _find_internal(img, (7, 3))
    if internal is None:
        return None
    return _outer_from_homography(internal, list(range(1, 8)), [3, 4, 5])


def _detect_quad(img):
    raise NotImplementedError(
        "outline/Hough fallbacks removed: they returned garbage corners and "
        "caused calibration jumps. Use detect_corners(); it falls back to the "
        "partial-window lattice solver instead.")


# ---------------------------------------------------------------------------
# Partial-window lattice solver
#
# When pieces stand on the board, findChessboardCorners frequently finds only
# a *sub-window* of the internal corner lattice (piece shadows straddle grid
# lines).  The classic middle-band path throws those away because it demands
# the full (7,3) grid line band.  Here we accept any window and resolve its
# integer offset on the 7x7 internal lattice:
#
#   * hypothesis = (window -> lattice orientation, x-shift, y-shift),
#   * each hypothesis defines a full-board homography (ideal 0..8 grid ->
#     pixels), scored by how well the 64 warped squares reproduce *a*
#     checkerboard parity (off-board cells dilute the score),
#   * because an integer slide of the lattice keeps parity valid, the score
#     alone cannot distinguish offsets of 2; a *second* window found elsewhere
#     on the board resolves it — only the true offset maps all windows'
#     corners onto near-integer lattice points inside the 1..7 grid at once.
# ---------------------------------------------------------------------------

_PATTERNS = [(7, 7), (7, 6), (7, 5), (7, 4), (7, 3), (6, 5), (6, 4), (6, 3),
             (5, 5), (5, 4), (5, 3), (4, 4), (4, 3), (3, 3)]

PARITY_FLOOR = 0.70    # min checkerboard agreement to accept a hypothesis
EDGE_WEIGHT = 0.5      # weight of the (normalised) board-edge evidence
ANCHOR_DIST = 0.30     # max (in squares) other windows' corners may sit off
                       # the chosen lattice
MIN_WINDOWS = 2        # a lone window cannot resolve even-square slides
MIN_MERGED = 12        # min distinct lattice corners for a trustworthy fit


_WINDOWS_BUDGET_S = 2.5      # s of wall clock for the partial-window search


def _prep_variants(grey):
    """Contrast variants for the window search.  Background-division (pixel /
    heavily-blurred pixel) cancels lighting gradients that otherwise break the
    adaptive threshold used by findChessboardCorners."""
    yield "raw", grey
    yield "histeq", cv2.equalizeHist(grey)
    g = grey.astype(np.float32)
    sig = max(101.0, float(min(grey.shape) / 2))
    bg = cv2.GaussianBlur(g, (0, 0), sig)
    yield "bgdiv", np.clip(g / (bg + 1e-6) * 128.0, 0, 255).astype(np.uint8)


def _detect_windows(img, max_windows=6):
    """Collect distinct internal-corner windows at full resolution.

    Returns a list of (pts (N,2) float32, (w, h)) sorted by size descending.
    Stops early once one large window plus an anchor window are found."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    windows = {}
    # wall-clock budget (2026-11-16): on a frame with no board this loop
    # crawled through 99 detector calls - 22 s on a desktop, 7 MINUTES on
    # the loaded Pi.  Return whatever was found once the budget is gone.
    deadline = time.monotonic() + _WINDOWS_BUDGET_S

    for prep_name, gg in _prep_variants(grey):
        for scale in (1.0, 0.75, 0.5):
            g = gg if scale >= 1.0 else cv2.resize(
                gg, (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA)
            for pat in _PATTERNS:
                if time.monotonic() > deadline:
                    return sorted(windows.values(),
                                  key=lambda t: -t[0].shape[0])[:max_windows]
                if prep_name == "raw" and pat in ((7, 7), (7, 3)):
                    continue          # stage-1 already tried these
                found, corners = cv2.findChessboardCorners(
                    g, pat,
                    cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE)
                if not found:
                    continue
                pts = corners.reshape(-1, 2).astype(np.float32) / scale
                # Sub-pixel accuracy wants the search window scaled to the
                # square size: a fixed (11,11) is tuned for small squares and
                # underfits 80px+ squares (the board fills more of the frame).
                pw = pat[0]
                if pw > 1:
                    sp = np.median(np.linalg.norm(
                        pts[1:pw] - pts[:pw - 1], axis=1))
                    win = int(np.clip(sp / 4.0, 5, 25)) | 1
                else:
                    win = 11
                pts = cv2.cornerSubPix(grey, pts, (win, win), (-1, -1), criteria)
                x1, y1 = pts.min(axis=0)
                x2, y2 = pts.max(axis=0)
                key = (int(x1 // 8), int(y1 // 8), int(x2 // 8), int(y2 // 8))
                windows.setdefault(key, (pts, pat))
            best = max((p[0].shape[0] for p in windows.values()), default=0)
            if best >= 15 and len(windows) >= 2:
                return sorted(windows.values(), key=lambda t: -t[0].shape[0])
    out = sorted(windows.values(), key=lambda t: -t[0].shape[0])
    return out[:max_windows]


def _orientations(pw, ph):
    """Yield (ideal Nx2 array, 2x2 linear part) for the 8 dihedral mappings of
    a pw x ph window lattice, ideal min-coordinate normalised to 1."""
    src = np.float64([[x, y] for y in range(ph) for x in range(pw)])
    mats = []
    for flip in (False, True):
        for turns in range(4):
            L = np.eye(2)
            if flip:
                L = np.array([[-1.0, 0.0], [0.0, 1.0]])
            for _ in range(turns):
                L = np.array([[0.0, -1.0], [1.0, 0.0]]) @ L
            mats.append(L)
    seen = set()
    for L in mats:
        ideal = src @ L.T
        ideal -= ideal.min(axis=0)
        ideal += 1.0
        sig = np.round(ideal, 3).tobytes()
        if sig in seen:
            continue
        seen.add(sig)
        yield ideal, L


def _ideal_to_pixel(ideal, offset, Linv, Hl):
    """Compose ideal-grid -> window-local -> pixel homography (3x3)."""
    shift = np.array([1.0, 1.0]) + np.asarray(offset, dtype=np.float64)
    A = np.eye(3)
    A[:2, :2] = Linv
    A[:2, 2] = -(Linv @ shift)
    return Hl @ A


def _parity_score(grey, H_full):
    """Fraction (0..1) of the 64 squares consistent with a checkerboard under
    this full-board homography.  Polarity-free (max of the two parities): the
    board's a1 convention is unknown to us, and the labelling orientation is
    settled later by _orient/A1-click.  Cells falling outside the image count
    as mismatches, so hypotheses sliding the lattice off the board score lower
    — an integer slide of a full square leaves the sampled pattern still
    checkerboard-consistent, only shifted onto part of the table."""
    size = 160
    S = np.array([[size / 8.0, 0, 0], [0, size / 8.0, 0], [0, 0, 1]])
    # warpPerspective takes src->dst; here source=pixels, dest=square grid:
    # pixel -> ideal (inv H_full) -> grid*unit (S).
    M = S @ np.linalg.inv(H_full)
    board = cv2.warpPerspective(grey, M, (size, size))
    inside = cv2.warpPerspective(np.full_like(grey, 255), M, (size, size))
    cells = cv2.resize(board, (8, 8), interpolation=cv2.INTER_AREA)
    cover = cv2.resize(inside, (8, 8), interpolation=cv2.INTER_AREA) / 255.0
    rr, cc = np.mgrid[0:8, 0:8]
    odd = ((rr + cc) % 2) == 1
    inb = cover > 0.9
    if not np.any(inb):
        return 0.0
    med = float(np.median(cells[inb]))
    m_odd = int(((cells < med) == odd)[inb].sum())
    n = int(inb.sum())
    return float(max(m_odd, n - m_odd) / 64.0)


def _edge_score(grey, H_full):
    """Weakest board-edge transition (grey levels) across the four lattice
    sides, or None if too few sides are measurable inside the frame.

    Checkerboard parity cannot tell the true lattice placement from one slid
    by whole squares (a slid lattice is still a perfect checkerboard where it
    overlaps the board).  Edges can: the true placement shows a board/table
    transition on every side; a slid lattice leaves some side looking out onto
    uniform table on both strips (no transition) or onto continuing
    checkerboard (strong transition).  Taking the minimum across sides
    therefore rewards the true placement and penalises slides."""
    pad, cell, size = 24, 20, 208
    S = np.array([[cell, 0, pad], [0, cell, pad], [0, 0, 1]], np.float64)
    M = S @ np.linalg.inv(H_full)
    b = cv2.warpPerspective(grey, M, (size, size)).astype(np.float64)
    m = cv2.warpPerspective(np.full_like(grey, 255), M, (size, size))
    m = m.astype(np.float64) / 255.0

    def strip_mean(y1, y2, x1, x2):
        y1, y2 = max(0, y1), min(size, y2)
        x1, x2 = max(0, x1), min(size, x2)
        if y2 <= y1 or x2 <= x1 or m[y1:y2, x1:x2].mean() < 0.95:
            return None
        return float(b[y1:y2, x1:x2].mean())

    lo, hi = pad, pad + 8 * cell
    sides = {}
    for i in range(8):
        for side, (ia, ib, oa, ob) in enumerate((
                (lo + 4, lo + 16, lo - 16, lo - 4),        # top
                (hi - 16, hi - 4, hi + 4, hi + 16),        # bottom
        )):
            inn = strip_mean(ia, ib, pad + cell * i, pad + cell * (i + 1))
            out = strip_mean(oa, ob, pad + cell * i, pad + cell * (i + 1))
            if inn is not None and out is not None:
                sides.setdefault(side, []).append(abs(inn - out))
        for side, (ia, ib, oa, ob) in enumerate((
                (lo + 4, lo + 16, lo - 16, lo - 4),        # left
                (hi - 16, hi - 4, hi + 4, hi + 16),        # right
        )):
            inn = strip_mean(pad + cell * i, pad + cell * (i + 1), ia, ib)
            out = strip_mean(pad + cell * i, pad + cell * (i + 1), oa, ob)
            if inn is not None and out is not None:
                sides.setdefault(2 + side, []).append(abs(inn - out))
    stats = [float(np.median(v)) for v in sides.values() if len(v) >= 4]
    return min(stats) if stats else None


def _lattice_residual(H_full, pts):
    """Worst distance (in squares) of the window's corners to integer lattice
    points under H_full, rejecting corners outside the internal grid."""
    ideal = cv2.perspectiveTransform(
        pts.reshape(-1, 1, 2).astype(np.float64),
        np.linalg.inv(H_full)).reshape(-1, 2)
    bad = (ideal < 0.5).any(1) | (ideal > 7.5).any(1)
    if np.any(bad):
        return 99.0
    d = np.abs(ideal - np.round(ideal)).max(axis=1)
    return float(np.percentile(d, 90))


def _resolve_windows(img, windows):
    """Fit one full-board homography to the detected windows and return the
    ordered outer corners, or None.  See the section comment above."""
    if not windows:
        return None
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    w, h = grey.shape[1], grey.shape[0]

    # Candidate homographies per window, best score first.
    per_window = []
    for pts, (pw, ph) in windows:
        grid = pts.reshape(ph, pw, 2).astype(np.float64)
        Hl, inl = cv2.findHomography(
            np.float64([[x, y] for y in range(ph) for x in range(pw)]),
            grid.reshape(-1, 2), cv2.RANSAC, 3.0)
        if Hl is None or int(inl.ravel().sum()) < 6:
            continue
        cands = []
        for ideal, L in _orientations(pw, ph):
            Linv = np.linalg.inv(L)
            xext = int(ideal[:, 0].max())
            yext = int(ideal[:, 1].max())
            base = ideal - ideal.min(axis=0) + 1.0
            for s in range(0, 8 - xext):
                for t in range(0, 8 - yext):
                    H = _ideal_to_pixel(base, (s, t), Linv, Hl)
                    outer = cv2.perspectiveTransform(
                        np.float32([[0, 0], [8, 0], [8, 8], [0, 8]])
                        .reshape(-1, 1, 2), H).reshape(-1, 2)
                    if not _valid(outer, w, h):
                        continue
                    sc = _parity_score(grey, H)
                    if sc >= PARITY_FLOOR:
                        cands.append((sc, H))
        if cands:
            cands.sort(key=lambda r: -r[0])
            # Parity alone admits whole-square slides; refine the strongest
            # parity candidates with the edge evidence (costlier metric).
            refined = []
            for sc, H in cands[:24]:
                e = _edge_score(grey, H)
                if e is not None:
                    sc += EDGE_WEIGHT * min(e, 255.0) / 255.0
                refined.append((sc, H))
            refined.sort(key=lambda r: -r[0])
            per_window.append((pts, refined[:8]))
    if len(per_window) < MIN_WINDOWS:
        # With one window only, even-square slides are unresolvable (the
        # lattice can be shifted by whole squares and still fit it).  Return
        # None — a missed detection is safe; a half-board error is not.
        return None

    per_window.sort(key=lambda t: -t[1][0][0])
    anchor_pts, anchor_cands = per_window[0]
    others = [p for p, _ in per_window[1:]]

    winner = None
    for sc, H in anchor_cands:
        if all(_lattice_residual(H, pts) <= ANCHOR_DIST for pts in others):
            winner = H
            break
    if winner is None:
        return None

    # Merge: snap every window's corners onto the winning lattice, then fit a
    # single RANSAC homography over all matched internal corners.
    merged = {}
    for pts, _ in per_window:
        ideal = cv2.perspectiveTransform(
            pts.reshape(-1, 1, 2).astype(np.float64),
            np.linalg.inv(winner)).reshape(-1, 2)
        for (gx, gy), p in zip(ideal, pts):
            key = (int(round(gx)), int(round(gy)))
            if not (1 <= key[0] <= 7 and 1 <= key[1] <= 7):
                continue
            if abs(gx - key[0]) > 0.35 or abs(gy - key[1]) > 0.35:
                continue
            merged.setdefault(key, []).append(p)
    src, dst = [], []
    for (gx, gy), ps in merged.items():
        src.append([gx, gy])
        dst.append(np.mean(ps, axis=0))
    if len(src) < MIN_MERGED:
        return None
    Hfit, _ = cv2.findHomography(np.float64(src), np.asarray(dst, np.float64),
                                 cv2.RANSAC, 3.0)
    if Hfit is None:
        return None
    outer = cv2.perspectiveTransform(
        np.float32([[0, 0], [8, 0], [8, 8], [0, 8]]).reshape(-1, 1, 2),
        Hfit).reshape(-1, 2)
    outer = _order_clockwise(outer)
    return outer if _valid(outer, w, h) else None


def _lattice(peaks, n_lines=9):
    """Fit the 9 evenly-spaced lattice lines of a chessboard axis to the
    detected seam positions.  Robust to spurious peaks (the table's grain
    adds some) AND to missing ones (the grain eats some): every pair of peaks
    is tried as two of the 9 lines and the lattice most peaks agree with wins
    (2026-11-16 - the old first-to-last fit failed the moment ONE extra seam
    peak appeared anywhere in the profile, which is what kept the tone board
    from ever being detected).  Supported lines then snap onto their actual
    seam peak and the rest are interpolated, so slightly non-uniform
    (perspective) spacing still lands on the seams.  Returns the full 9-line
    list or None."""
    pk = np.array(sorted(int(p) for p in peaks), dtype=np.float64)
    if len(pk) < 4:
        return None
    best = None                       # (key, lines)
    for i in range(len(pk)):
        for j in range(i + 1, len(pk)):
            for d in range(1, n_lines):
                pitch = (pk[j] - pk[i]) / float(d)
                if not 12.0 <= pitch <= 320.0:
                    continue
                # every comb line around the peak range
                k0 = int(np.floor((pk[0] - pk[i]) / pitch)) - 1
                k1 = int(np.ceil((pk[-1] - pk[i]) / pitch)) + 1
                comb = pk[i] + np.arange(k0, k1 + 1) * pitch
                if len(comb) < n_lines:
                    continue
                tol = 0.25 * pitch
                dmat = np.abs(pk[:, None] - comb[None, :])
                near = dmat <= tol     # a peak is near at most one line
                lin_has = near.any(axis=0)
                dmin = dmat.min(axis=1)
                inl = dmin <= tol
                if int(inl.sum()) < 6:
                    continue
                nearest = dmat.argmin(axis=1)
                lin_err = np.bincount(nearest[inl], weights=dmin[inl],
                                      minlength=len(comb))
                csup = np.concatenate(([0], lin_has.astype(np.int32).cumsum()))
                cerr = np.concatenate(([0], lin_err.cumsum()))
                win_sup = csup[n_lines:] - csup[:-n_lines]
                win_err = cerr[n_lines:] - cerr[:-n_lines]
                smax = int(win_sup.max())
                if smax < 6:
                    continue
                cand = np.flatnonzero(win_sup == smax)
                st = int(cand[np.argmin(win_err[cand])])
                outer = int(lin_has[st]) + int(lin_has[st + n_lines - 1])
                key = (smax, outer, -float(win_err[st]))
                if best is None or key > best[0]:
                    best = (key, comb[st:st + n_lines])
    if best is None:
        return None
    lines = best[1]
    # snap each line to its nearest unused seam peak (within a quarter pitch)
    pitch = float(lines[1] - lines[0])
    tol = 0.25 * pitch
    out = [None] * n_lines
    used = set()
    for k, ln in enumerate(lines):
        bp, bd_ = None, tol
        for pi, p in enumerate(pk):
            if pi in used:
                continue
            dd = abs(p - ln)
            if dd <= bd_:
                bp, bd_ = pi, dd
        if bp is not None:
            out[k] = float(pk[bp])
            used.add(bp)
    known = [k for k, v in enumerate(out) if v is not None]
    if len(known) < 2:
        return None
    # lines whose seam is missing: interpolate, extrapolating at the ends
    for k in range(n_lines):
        if out[k] is not None:
            continue
        before = [b for b in known if b < k]
        after = [a for a in known if a > k]
        if before and after:
            b, a = before[-1], after[0]
            out[k] = out[b] + (out[a] - out[b]) * (k - b) / float(a - b)
        elif len(before) >= 2:
            b1, b2 = before[-1], before[-2]
            out[k] = out[b1] + (out[b1] - out[b2]) * (k - b1) / float(b1 - b2)
        elif len(after) >= 2:
            a1, a2 = after[0], after[1]
            out[k] = out[a1] + (out[a1] - out[a2]) * (k - a1) / float(a1 - a2)
        else:
            return None
    return out


def _line_peaks(mask, axis):
    """The line positions of a seam mask along one axis (the seams are
    closed first so the dashed grain gaps do not split a line)."""
    kern = (1, 25) if axis == 0 else (25, 1)
    closed = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, kern))
    v = closed.sum(axis=axis).astype(float)
    thr = float(v.max()) * 0.25      # the table's grain raises the max
    out = []
    for i in range(1, len(v) - 1):
        if v[i] > thr and v[i] >= v[i - 1] and v[i] >= v[i + 1]:
            if not out or i - out[-1] > 50:   # one peak per seam
                out.append(i)
    return out


def _seam_saddles(plane, pts, cell):
    """How many of the lattice's internal intersections look like real
    checker corners (0..1).  At a true corner the two diagonal quadrant
    pairs match each other and differ from the other pair; a lattice whose
    LINES drift off the seams scores near 0 even when its squares still
    alternate (2026-11-16: a wrong-pitch lattice over a printed checker
    kept parity at 1.0 - the cell medians keep alternating while the lines
    slide off the corners; the saddle score saw it at 0.33 vs 0.88)."""
    v = pts.reshape(7, 7, 2)
    r = max(3, int(cell / 6))
    m = r // 2
    ok = tot = 0
    for y in range(7):
        for x in range(7):
            cx, cy = int(v[y][x][0]), int(v[y][x][1])
            x0, x1, y0, y1 = cx - r, cx + r, cy - r, cy + r
            if x0 < 0 or y0 < 0 or x1 >= plane.shape[1] or y1 >= plane.shape[0]:
                continue
            tl = float(plane[y0:cy - m, x0:cx - m].mean())
            tr = float(plane[y0:cy - m, cx + m:x1].mean())
            bl = float(plane[cy + m:y1, x0:cx - m].mean())
            br = float(plane[cy + m:y1, cx + m:x1].mean())
            sep = abs((tl + br) - (tr + bl)) / 2.0
            spread = max(abs(tl - br), abs(tr - bl), 1.0)
            tot += 1
            if sep > spread:
                ok += 1
    return ok / float(tot) if tot else 0.0


def _seam_parity(plane, pts):
    """How checkerboard-like the lattice's squares actually are (0..1):
    the share of adjacent square pairs that agree on light/dark parity.
    A seam grid in ordinary wood scores near 0.5; the gold board's tone
    parity scores ~0.98 even though its brightness contrast is tiny.
    `plane` is any single-channel image the families separate in - grey,
    LAB-a*, ... - and the score is polarity-free (2026-11-16): WHICH family
    reads higher depends on the board and the light, so both assignments are
    scored and the better one kept."""
    v = pts.reshape(7, 7, 2)
    cells = []
    for r in range(8):
        row = []
        for c in range(8):
            # the cell's corners from the lattice (extrapolating the edges)
            def gx(rr, cc):
                rr = min(6, max(0, rr))
                cc = min(6, max(0, cc))
                return v[rr][cc]
            p = [gx(r - 1, c - 1), gx(r - 1, c), gx(r, c - 1), gx(r, c)]
            xs = [q[0] for q in p]
            ys = [q[1] for q in p]
            x0, x1 = int(min(xs) + 6), int(max(xs) - 6)
            y0, y1 = int(min(ys) + 6), int(max(ys) - 6)
            if x1 <= x0 or y1 <= y0 or y0 < 0 or x0 < 0 \
                    or y1 >= plane.shape[0] or x1 >= plane.shape[1]:
                row.append(None)
                continue
            row.append(float(np.median(plane[y0:y1, x0:x1])))
        cells.append(row)
    ok = tot = 0
    for r in range(8):
        for c in range(8):
            for dr, dc in ((0, 1), (1, 0)):
                rr, cc = r + dr, c + dc
                if rr < 8 and cc < 8 and cells[r][c] is not None \
                        and cells[rr][cc] is not None:
                    tot += 1
                    if (cells[r][c] > cells[rr][cc]) == ((r + c) % 2 == 0):
                        ok += 1
    if not tot:
        return 0.0
    sc = ok / float(tot)
    return max(sc, 1.0 - sc)


def _detect_seam_lattice(img):
    """Thin-dark-seam lattice (2026-11-03).

    A board whose squares differ in WOOD TONE rather than light/dark (the
    gold board) has no usable checkerboard contrast: the grain swings more
    within a square than the parity does between squares, so every
    checkerboard detector - classic or SB - sees only noise.  The SEAMS
    between the squares, however, binarise cleanly.  Extract the seam grid,
    fit the 9x9 lattice through it (tolerating lines the grain ate), and
    intersect it into the 7x7 internal corners.

    2026-11-16: each axis's profile is re-extracted INSIDE the other axis's
    board extent and refitted - the table's clutter beside the board puts
    seam-like peaks along the board's own rows/columns and the polluted
    profile fit the wrong lattice (on the real failing frame the vertical
    fit was right while the horizontal one locked onto the table and
    squashed the board by 50 px)."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # the seams are thin dark lines: the adaptive threshold sees them (the
    # square tones are too close to split, and that is exactly the point)
    th = cv2.adaptiveThreshold(cv2.GaussianBlur(grey, (0, 0), 3), 255,
                               cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 31, 5)
    seams = 255 - th                  # seam pixels are white now
    vert = _lattice(_line_peaks(seams, 0))
    horiz = _lattice(_line_peaks(seams, 1))
    for _ in range(2):
        if vert is None or horiz is None:
            break
        band = seams.copy()           # columns of the board only
        x0 = max(0, int(vert[0]) - 12)
        band[:, :x0] = 0
        band[:, int(vert[-1]) + 13:] = 0
        h2 = _lattice(_line_peaks(band, 1))
        band = seams.copy()           # rows of the board only
        y0 = max(0, int(horiz[0]) - 12)
        band[:y0] = 0
        band[int(horiz[-1]) + 13:] = 0
        v2 = _lattice(_line_peaks(band, 0))
        vert, horiz = (v2 or vert), (h2 or horiz)
    if vert is None or horiz is None:
        return None
    # the 7x7 INTERNAL corners: the inner grid lines' intersections
    vx, hy = vert[1:-1], horiz[1:-1]
    pts = np.array([[x, y] for y in hy for x in vx], dtype=np.float32)
    cv2.cornerSubPix(grey, pts, (11, 11), (-1, -1),
                     (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                      30, 0.001))
    # the lattice must BE a checkerboard, not just a grid, and sit ON it.
    # Two gates, each scored on every plane the families may separate in -
    # greyscale AND the LAB chroma planes (2026-11-16: the tone board's
    # families OVERLAP in greyscale, gap 20.6 vs spread 13.7, but split
    # cleanly in chroma, a* ratio 2.0 - grey-only parity rejected the very
    # board this path was built for):
    #   parity  - ordinary wood seams score near 0.5, tone boards high.
    #   saddles - the lines must meet at the real corners; a wrong-pitch
    #             lattice on the plank-grain tables kept parity at 1.0.
    # Below either gate the classic paths get their turn, exactly as before.
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    planes = (grey, lab[:, :, 1], lab[:, :, 2])
    if max(_seam_parity(pl, pts) for pl in planes) < 0.70:
        return None
    cell = ((vert[-1] - vert[0]) + (horiz[-1] - horiz[0])) / 16.0
    if max(_seam_saddles(pl, pts, cell) for pl in planes) < 0.80:
        return None
    # the outer corners are the OUTER SEAM LINES' intersections - directly
    # observed (2026-11-16).  The homography extrapolation the window path
    # needs ran ~10 px wide here: it projects the internal grid outward and
    # the lens distortion beyond the grid bends the real corners back in.
    corners = np.array([[vert[0], horiz[0]], [vert[-1], horiz[0]],
                        [vert[-1], horiz[-1]], [vert[0], horiz[-1]]],
                       dtype=np.float32)
    return _order_clockwise(corners)


_DEBUG_FAILS = [0]


def _debug_dump(img):
    """Keep a failing frame and say which stage gave up (2026-11-04): the
    live failures were invisible without a look at the frame. Every 30th
    failure, so a stuck loop does not fill the disk."""
    _DEBUG_FAILS[0] += 1
    if _DEBUG_FAILS[0] % 30 != 1:
        return
    try:
        path = "/tmp/board_detect_fail.png"
        cv2.imwrite(path, img)
        grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        th = cv2.adaptiveThreshold(cv2.GaussianBlur(grey, (0, 0), 3), 255,
                                   cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 31, 5)
        seams = 255 - th
        vp = _line_peaks(seams, 0)
        hp = _line_peaks(seams, 1)
        vl, hl = _lattice(vp), _lattice(hp)
        pts = None
        try:
            pts = _detect_seam_lattice(img)
        except Exception:
            pass
        print("board_autodetect: no board found - seam peaks v=%d h=%d | "
              "lattice v=%s h=%s | validated=%s | frame saved to %s"
              % (len(vp), len(hp), "ok" if vl else "no",
                 "ok" if hl else "no", "ok" if pts is not None else "no",
              path), flush=True)
    except Exception as e:
        print("board_autodetect: debug dump failed:", e, flush=True)


def detect_corners(img):
    """Return the 4 board corners (ordered, pixel coords) or None."""
    h, w = img.shape[:2]
    # Stage 1: the seam lattice FIRST (2026-11-04). It costs ~20 ms and is
    # the only thing that sees a tone board; the classic checkerboard
    # attempts cost ~1 s per frame and were being paid BEFORE it, which made
    # the calibration take minutes. The parity validation keeps ordinary
    # wood from false-triggering, so the classic paths still get their turn.
    try:
        corners = _detect_seam_lattice(img)   # ordered corners or None
    except Exception:
        corners = None
    if corners is not None and _valid(corners, w, h):
        return corners
    # Stage 2: classic full-band paths (exact when they work).
    for fn in (_detect_chessboard, _detect_middle_band):
        try:
            corners = fn(img)
        except Exception:
            corners = None
        if corners is not None and _valid(corners, w, h):
            return corners
    # Stage 3: partial-window lattice solver (shadows / glare / big squares).
    try:
        corners = _resolve_windows(img, _detect_windows(img))
    except Exception:
        corners = None
    if corners is not None and _valid(corners, w, h):
        return corners
    _debug_dump(img)
    return None


def _valid(corners, w, h):
    """Reject obviously bogus corners (out of frame or degenerate)."""
    for x, y in corners:
        if not (-0.25 * w <= x <= 1.25 * w and -0.25 * h <= y <= 1.25 * h):
            return False
    area = cv2.contourArea(np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2))
    return area > 0.005 * w * h


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

def _warp_labeling(img, a1, h1, a8, h8, d=400):
    """Warp the board so a8->top-left, h8->top-right, h1->bottom-right,
    a1->bottom-left.  Row 0 is then rank 8 (black) and row 7 is rank 1."""
    src = np.float32([a8, h8, h1, a1])
    dst = np.float32([[0, 0], [d, 0], [d, d], [0, d]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (d, d))


def _score_warp(warped):
    """Score a candidate orientation.

    Two independent signals:

    1. Pieces: white pieces (bright) sit on ranks 1-2, black pieces (dark) on
       ranks 7-8, so the bottom band should be brighter than the top band.
    2. Squares: the middle four ranks are empty, and a1 (bottom-left) is dark,
       so dark squares satisfy (row+col) odd.  Reflection flips this parity.

    The piece term disambiguates rank 1 vs rank 8; the parity term disambiguates
    the a-file vs the h-file.  (On an empty board there are no pieces, so the
    two 180-degree-rotated orientations tie — that is the expected ambiguity.)
    """
    d = warped.shape[0]
    grey = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    sq = d // 8

    def sqmean(r, c):
        r1 = r * sq + sq // 4
        r2 = (r + 1) * sq - sq // 4
        c1 = c * sq + sq // 4
        c2 = (c + 1) * sq - sq // 4
        return float(np.mean(grey[r1:r2, c1:c2]))

    white_band = float(np.mean(grey[6 * sq:8 * sq, :]))
    black_band = float(np.mean(grey[0:2 * sq, :]))
    piece_score = white_band - black_band

    vals = [sqmean(r, c) for r in range(2, 6) for c in range(8)]
    med = float(np.median(vals))
    parity = 0
    for r in range(2, 6):
        for c in range(8):
            dark = sqmean(r, c) < med
            expected_dark = ((r + c) % 2) == 1
            parity += 1 if dark == expected_dark else -1

    return piece_score + 2.0 * parity


def _orient(img, corners):
    """Assign A1/H1/A8/H8 to the 4 cyclic corners using the best of the 8
    possible labelings (4 rotations x mirror)."""
    best = None
    best_score = -np.inf
    for rev in (False, True):
        seq = [corners[0], corners[3], corners[2], corners[1]] if rev else list(corners)
        for i in range(4):
            rot = seq[i:] + seq[:i]
            a1, h1, h8, a8 = rot[0], rot[1], rot[2], rot[3]
            warped = _warp_labeling(img, a1, h1, a8, h8)
            score = _score_warp(warped)
            if score > best_score:
                best_score = score
                best = {"A1": tuple(a1), "H1": tuple(h1),
                        "A8": tuple(a8), "H8": tuple(h8)}
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_board(img):
    """Detect the board and return labelled corners normalised to [0,1]."""
    corners = detect_corners(img)
    if corners is None:
        return None
    labelled = _orient(img, corners)
    h, w = img.shape[:2]
    return {k: (float(x) / w, float(y) / h) for k, (x, y) in labelled.items()}


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "images/1.jpg"
    img = cv2.imread(path)
    if img is None:
        print("cannot read", path)
        sys.exit(1)
    result = detect_board(img)
    if result is None:
        print("detection failed")
        sys.exit(1)
    for k in ("A1", "H1", "A8", "H8"):
        print("%s %.4f %.4f" % (k, result[k][0], result[k][1]))
