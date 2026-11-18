# verify_log.py - persist an Arm-grab verify session to disk for offline review.
#
# A calibration that "looks wrong but I can't say why" is unreproducible by
# definition (write-only link, hand-judged grabs), so the session log IS the
# diagnostic. Files land under data/ (device state, excluded from deploy so they
# survive redeploys and never clobber the golden config):
#
#   data/verify_last.json   full current session (flushed after every event)
#   data/verify_last.txt    same, human-readable (cat / scp to review by eye)
#   data/verify_history.jsonl  one compact line per completed session (capped)
#
# All writes are atomic (tmp + os.replace). No widgets, no imports of the UI -
# the session is a plain dict the caller mutates.

import hashlib
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
LAST_JSON = os.path.join(DATA, "verify_last.json")
LAST_TXT = os.path.join(DATA, "verify_last.txt")
HISTORY = os.path.join(DATA, "verify_history.jsonl")
HISTORY_CAP = 50

# The five fitted constants that a "candidate" consists of.
CONST_KEYS = ("radscale", "stickout", "gfudge", "pickheightadj",
              "pickheightreachslope")
# Rounding used for the stall fingerprint (a candidate that maps to an already-
# seen fingerprint is the loop spinning its wheels, not making progress).
_FP_DP = {"radscale": 3, "stickout": 2, "gfudge": 1, "pickheightadj": 2,
          "pickheightreachslope": 4}


def fingerprint(consts):
    """Stable short id for a candidate's five constants, rounded to the dp in
    _FP_DP so trivial float jitter doesn't read as a 'new' config."""
    return "|".join("%.*f" % (_FP_DP[k], float(consts.get(k, 0.0)))
                    for k in CONST_KEYS)


def board_context():
    """The full model context at run time, so a reviewer can re-derive the
    commanded poses offline (self-describing, like probes.json)."""
    import CBstate
    try:
        with open(CBstate.mydir + "points.txt", "rb") as f:
            fp = hashlib.md5(f.read()).hexdigest()
    except OSError:
        fp = None
    return {
        "points_fp": fp,
        "squaresize": CBstate.AL5D_squaresize,
        "axistorow0": CBstate.AL5D_axistorow0,
        "flip_rank": int(bool(CBstate.AL5D_flip_rank)),
        "flip_file": int(bool(CBstate.AL5D_flip_file)),
        "A": CBstate.AL5D_A,
        "B": CBstate.AL5D_B,
        "cruise_height": CBstate.AL5D_cruise_height,
    }


def new_session():
    return {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context": board_context(),
        "guided": [],          # the guided-5 base samples (square, X, Y, Z)
        "candidates": [],      # one per fit applied: {fp, consts, scatter_in, n_recs}
        "tests": [],           # one per verify attempt
        "flags": [],           # advisories, e.g. model_gap
        "outcome": "in-progress",
        "refits": 0,
        "attempts": 0,
    }


def add_guided(session, rec):
    session["guided"].append({"square": rec["square"],
                              "X": round(float(rec["X"]), 3),
                              "Y": round(float(rec["Y"]), 3),
                              "Z": round(float(rec["Z"]), 3)})


def add_candidate(session, consts, scatter_in, n_recs):
    fp = fingerprint(consts)
    session["candidates"].append({
        "fp": fp,
        "consts": {k: float(consts.get(k, 0.0)) for k in CONST_KEYS},
        "scatter_in": round(float(scatter_in), 3),
        "n_recs": int(n_recs),
        "ts": time.strftime("%H:%M:%S"),
    })
    return fp


def add_test(session, square, quadrant, candidate_fp, scatter_in,
             predicted, corrected, verdict):
    """predicted / corrected are dicts {X,Y,Z} (corrected None if not re-jogged).
    err is the board-inch decomposition of corrected-predicted, for a reviewer to
    see the failure MODE (reach vs lateral vs height)."""
    err = _err_inches(predicted, corrected)
    session["attempts"] += 1
    session["tests"].append({
        "square": square, "quadrant": quadrant, "candidate_fp": candidate_fp,
        "scatter_in": round(float(scatter_in), 3) if scatter_in is not None else None,
        "predicted": _rd(predicted), "corrected": _rd(corrected),
        "err": err, "verdict": verdict,
        "ts": time.strftime("%H:%M:%S"),
    })


def add_flag(session, kind, note):
    session["flags"].append({"kind": kind, "note": note,
                             "ts": time.strftime("%H:%M:%S")})


def _norm(pose):
    """Accept a pose as a dict {X,Y,Z} OR a [X, Y, Z] list/tuple (callers pass
    self.origin, which is a list). Returns None for a falsy pose."""
    if not pose:
        return None
    if isinstance(pose, dict):
        return pose
    x, y, z = pose
    return {"X": x, "Y": y, "Z": z}


def _rd(pose):
    p = _norm(pose)
    if not p:
        return None
    return {k: round(float(p[k]), 3) for k in ("X", "Y", "Z")}


def _err_inches(predicted, corrected):
    """Board-inch split of the jog correction. reach = radial in/out, lateral =
    arc = r*dtheta at that reach, height = dip/lift. Null when no correction was
    recorded (e.g. a Good verdict or an unreachable attempt)."""
    predicted = _norm(predicted)
    corrected = _norm(corrected)
    if not predicted or not corrected:
        return None
    import math
    r = float(predicted.get("X", 0.0))
    return {
        "reach_in": round(float(corrected["X"]) - float(predicted["X"]), 3),
        "lateral_in": round(r * math.radians(
            float(corrected["Z"]) - float(predicted["Z"])), 3),
        "height_in": round(float(corrected["Y"]) - float(predicted["Y"]), 3),
    }


def human_text(session):
    s = session
    c = s["context"]
    lines = [
        "Arm-grab verify session  %s" % s["started"],
        "outcome=%s  refits=%d  attempts=%d" % (s["outcome"], s["refits"],
                                                s["attempts"]),
        "board sq=%.3f axis=%.3f flips=%d/%d  A=%.3f B=%.3f cruise=%.2f  pts=%s"
        % (c["squaresize"], c["axistorow0"], c["flip_rank"], c["flip_file"],
           c["A"], c["B"], c["cruise_height"], (c.get("points_fp") or "?")[:8]),
        "",
        "candidates (fp  scatter_in  n_recs):",
    ]
    for cd in s["candidates"]:
        lines.append("  %-32s %6.3f in  recs=%d  @%s"
                     % (cd["fp"], cd["scatter_in"], cd["n_recs"], cd["ts"]))
    lines.append("")
    lines.append("tests (verdict  reach/lateral/height err in  cand):")
    if not s["tests"]:
        lines.append("  (none)")
    for t in s["tests"]:
        e = t.get("err")
        es = ("reach%+.2f lat%+.2f hgt%+.2f" % (e["reach_in"], e["lateral_in"],
                                                e["height_in"]) if e else "n/a")
        lines.append("  %-3s [%s] %-9s %s  @%s"
                     % (t["square"], t["quadrant"], t["verdict"], es,
                        t["candidate_fp"][:20]))
    if s["flags"]:
        lines.append("")
        lines.append("flags:")
        for f in s["flags"]:
            lines.append("  %s: %s" % (f["kind"], f["note"]))
    return "\n".join(lines) + "\n"


def flush(session):
    """Write verify_last.{json,txt}. Cheap; call after every event so a crash
    mid-session still leaves the full history of attempts on disk."""
    try:
        os.makedirs(DATA, exist_ok=True)
        _atomic(LAST_JSON, json.dumps(session, indent=1))
        _atomic(LAST_TXT, human_text(session))
        return True
    except OSError:
        return False


def finalize(session, outcome):
    """Set the outcome, flush, and append a one-line summary to the rolling
    history (capped) so cross-session trends are visible offline."""
    session["outcome"] = outcome
    flush(session)
    try:
        lines = []
        if os.path.exists(HISTORY):
            with open(HISTORY) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        c = session["context"]
        best = min((cd["scatter_in"] for cd in session["candidates"]),
                   default=None)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "outcome": outcome,
            "sq": c["squaresize"],
            "refits": session["refits"], "attempts": session["attempts"],
            "good": sum(1 for t in session["tests"] if t["verdict"] == "good"),
            "quads": len({t["quadrant"] for t in session["tests"]
                          if t["verdict"] == "good"}),
            "min_scatter_in": best,
            "flags": sorted({f["kind"] for f in session["flags"]}),
            "points_fp": (c.get("points_fp") or "")[:12],
        }
        lines.append(json.dumps(rec))
        _atomic(HISTORY, "\n".join(lines[-HISTORY_CAP:]) + "\n")
        return True
    except OSError:
        return False


def _atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
