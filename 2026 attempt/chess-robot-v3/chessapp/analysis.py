# chessapp/analysis.py - post-game analysis: replay a game, run Stockfish over
# every position, and derive the FACTS a narrator needs (move quality, turning
# points, decision points, quiet stretches). Pure offline; no arm/robot imports.
#
#   python3 -m chessapp.analysis games/game_1233404.pgn
#
# The result is JSON (cached under data/) and is the input to narrative.py.
import json
import os
import shutil
import subprocess

import chess
import chess.pgn

from chessapp import chessfacts as CF
from chessapp import chesspos as CP
from chessapp import openings as OP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATE_CP = 10000            # converted mate score for arithmetic
PIECE_VALUE = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9, "k": 0}

# loss thresholds (centipawns, from the mover's point of view)
T_INACCURACY = 35
T_MISTAKE = 90
T_BLUNDER = 180
T_GREAT_GAP = 60           # best move must beat #2 by this to be "great"
T_TURN = 120               # swing that deserves a turning-point callout


def find_engine():
    return shutil.which("stockfish") or "/usr/games/stockfish"


class Engine:
    """Minimal UCI client - enough to ask 'evaluate this position'."""

    def __init__(self, path=None, multipv=3):
        self.path = path or find_engine()
        self.multipv = multipv
        self.p = subprocess.Popen(
            [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
            # low priority: analysis runs for minutes and must never make
            # the touchscreen feel dead (2026-10-22)
            preexec_fn=lambda: os.nice(10))
        self._put("uci")
        self._wait("uciok")
        self._put("setoption name MultiPV value %d" % multipv)
        self._put("setoption name UCI_ShowWDL value true")
        tb = os.path.join(ROOT, "data", "tablebases")
        if os.path.isdir(tb):
            self._put("setoption name SyzygyPath value %s" % tb)
        self._put("isready")
        self._wait("readyok")

    def _put(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _wait(self, token):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine died waiting for %s" % token)
            if line.startswith(token):
                return line.strip()

    def set_multipv(self, n):
        self._put("setoption name MultiPV value %d" % n)
        self._put("isready")
        self._wait("readyok")

    def analyse(self, fen, depth=13, multipv=None):
        """[{uci, cp, mate, wdl, pv}] best first, scores in WHITE POV."""
        if multipv is not None:
            self.set_multipv(multipv)
        self._put("ucinewgame")
        self._put("isready")
        self._wait("readyok")
        self._put("position fen " + fen)
        self._put("go depth %d" % depth)
        turn = chess.WHITE if fen.split()[1] == "w" else chess.BLACK
        best = {}
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine died mid-search")
            line = line.strip()
            if line.startswith("bestmove"):
                break
            rec = self._parse_info(line, turn)
            if rec:
                best[rec["multipv"]] = rec   # keep the deepest per line
        return [best[k] for k in sorted(best)]

    @staticmethod
    def _parse_info(line, turn):
        parts = line.split()
        try:
            if "pv" not in parts or "multipv" not in parts:
                return None
            mp = int(parts[parts.index("multipv") + 1])
            si = parts.index("score")
            kind, val = parts[si + 1], int(parts[si + 2])
        except (ValueError, IndexError):
            return None
        if kind == "mate":
            mate = val
            cp = (MATE_CP - 100 * min(abs(val), 40)) * (1 if val > 0 else -1)
        else:
            mate, cp = None, val
        wdl = None
        if "wdl" in parts:
            try:
                i = parts.index("wdl")
                wdl = (int(parts[i + 1]), int(parts[i + 2]), int(parts[i + 3]))
            except (ValueError, IndexError):
                wdl = None
        if turn == chess.BLACK:               # UCI scores are side-to-move
            cp = -cp
            if mate is not None:
                mate = -mate
            if wdl is not None:
                wdl = (wdl[2], wdl[1], wdl[0])
        return {"multipv": mp, "cp": cp, "mate": mate, "wdl": wdl,
                "pv": parts[parts.index("pv") + 1:]}

    def close(self):
        try:
            self._put("quit")
            self.p.wait(timeout=3)
        except Exception:
            pass


def _material(board):
    total = 0
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece:
            total += PIECE_VALUE[piece.symbol().lower()] * (
                1 if piece.color == chess.WHITE else -1)
    return total


def _concept_deltas(rep_before, rep_after, turn):
    """Change per concept from the MOVER's point of view: their own gain
    plus the opponent's loss. Positive = the move improved that aspect.
    (CCC's prioritization idea: commentary leads with what changed most.)"""

    def side(v):
        if isinstance(v, tuple):        # (passed, isolated, doubled) pawns
            return 2 * v[0] - v[1] - v[2]
        return int(v)

    sign = 1 if turn == chess.WHITE else -1
    out = {}
    for k, v in rep_before.items():
        a = rep_after[k]
        if isinstance(v, tuple):
            w = side(a[0]) - side(v[0]) - (side(a[1]) - side(v[1]))
        else:
            w = side(a) - side(v)
        out[k] = int(round(w * sign))
    return out


def load_pgn(path):
    with open(path) as f:
        game = chess.pgn.read_game(f)
    if game is None:
        raise ValueError("no game found in %s" % path)
    return game


def _eval_words(cp):
    """Human phrasing of an evaluation (WHITE point of view)."""
    if cp is None:
        return "unclear"
    a = abs(cp)
    if a >= MATE_CP - 4000:
        return "a forced win for %s" % ("White" if cp > 0 else "Black")
    if a < 40:
        return "an equal position"
    if a < 110:
        return "a slight edge for %s" % ("White" if cp > 0 else "Black")
    if a < 250:
        return "a clear advantage for %s" % ("White" if cp > 0 else "Black")
    if a < 500:
        return "a winning advantage for %s" % ("White" if cp > 0 else "Black")
    return "a completely winning position for %s" % (
        "White" if cp > 0 else "Black")


def _pv_line(board, pv, mover, mate=None):
    """The recommended line as speakable data: up to 3 full moves (6 plies),
    the named events along it, and where the gain lands - 'winning the queen
    with bishop to b2' is built from this."""
    b = board.copy()
    plies = []
    for u in (pv or [])[:6]:
        try:
            mv = chess.Move.from_uci(u)
        except Exception:
            break
        san = b.san(mv)
        kinds = [k for k, _ in CF.move_facts(b, mv)]
        plies.append({"san": san,
                      "color": "white" if b.turn == chess.WHITE else "black",
                      "forced": b.legal_moves.count() == 1,
                      "kinds": kinds, "capture": b.is_capture(mv),
                      "check": b.gives_check(mv)})
        b.push(mv)
    total = (_material(b) - _material(board)) * (1 if mover == chess.WHITE
                                                 else -1)
    # where does the line's gain materialize?
    run = board.copy()
    cum, best_at, best_jump = 0.0, None, 0.0
    for i, pl in enumerate(plies):
        mv = chess.Move.from_uci((pv or [])[i])
        before = _material(run)
        run.push(mv)
        swing = (_material(run) - before) * (1 if mover == chess.WHITE else -1)
        cum += swing
        if swing > best_jump:
            best_jump, best_at = swing, i
    concl = {"kind": "holds", "what": "", "at": None, "move": ""}
    if mate is not None and mate > 0:
        concl = {"kind": "mate", "what": "", "at": len(plies) - 1,
                 "move": plies[-1]["san"] if plies else ""}
    elif total >= 8:
        concl = {"kind": "wins", "what": "the queen", "at": best_at,
                 "move": plies[best_at]["san"] if best_at is not None else ""}
    elif total >= 3:
        concl = {"kind": "wins", "what": "the exchange", "at": best_at,
                 "move": plies[best_at]["san"] if best_at is not None else ""}
    elif total >= 1:
        concl = {"kind": "wins", "what": "a pawn", "at": best_at,
                 "move": plies[best_at]["san"] if best_at is not None else ""}
    return {"plies": plies, "total": total, "concl": concl}


def _pv_impact(board, pv, mover, best_mate=None):
    """What the recommended line achieves (the short form)."""
    line = _pv_line(board, pv, mover, best_mate)
    c = line["concl"]
    if c["kind"] == "wins":
        return {"kind": "wins", "what": c["what"] or "material"}
    return {"kind": "keeps", "what": ""}


def _impact_of(line):
    """Short consequence form of a recommended line (for 'which would have..')."""
    c = line["concl"]
    if c["kind"] == "mate":
        return {"kind": "wins", "what": "the game"}
    if c["kind"] == "wins":
        return {"kind": "wins", "what": c["what"] or "material"}
    return {"kind": "keeps", "what": ""}


def _player_side(headers):
    """Which side the human plays, taken from the game itself: a robot/engine
    name on one side means the other side is the player."""
    w = (headers.get("White") or "").lower()
    b = (headers.get("Black") or "").lower()
    marks = ("robot", "engine", "stockfish", "computer", "pi", "al5")
    w_bot = any(k in w for k in marks)
    b_bot = any(k in b for k in marks)
    if w_bot and not b_bot:
        return "black"
    if b_bot and not w_bot:
        return "white"
    return None


def _phase_markers(board_start, entries):
    """Opening -> middlegame -> endgame transition points, each with a
    position report at the moment of the change."""
    out = []
    b = board_start.copy()
    mid = False
    for e in entries:
        b.push(chess.Move.from_uci(e["uci"]))
        if not mid and (e["ply"] >= 20
                        or (_castled(b, chess.WHITE)
                            and _castled(b, chess.BLACK))):
            mid = True
            out.append({"ply": e["ply"], "to": "middlegame",
                        "report": CP.report(b)})
        elif mid and _is_endgame(b):
            out.append({"ply": e["ply"], "to": "endgame",
                        "report": CP.report(b)})
            break
    return out


def _castled(board, color):
    """True when the king sits on its castled square."""
    k = board.king(color)
    return k in (chess.G1, chess.C1) if color == chess.WHITE else \
        k in (chess.G8, chess.C8)


def _is_endgame(board):
    def n_types(*types):
        return sum(1 for sq in chess.SQUARES
                   if (lambda p: p is not None and p.piece_type in types)
                   (board.piece_at(sq)))
    return (n_types(chess.QUEEN) == 0
            or n_types(chess.ROOK, chess.BISHOP, chess.KNIGHT) <= 3)


def analyze_game(game, depth=18, multipv=3, engine=None, progress=None):
    """Walk the game with the engine and return the analysis dict.

    TWO passes per position (2026-10-22): MultiPV 1 at full depth for the
    verdict (the eval and the best line - MultiPV splits search effort, so a
    verdict from a MultiPV-3 search is diluted), and a cheaper MultiPV pass
    for the candidate alternatives. Background pre-warming absorbs the time."""
    own = engine is None
    eng = engine or Engine(multipv=multipv)
    board = game.board()
    moves = list(game.mainline_moves())

    headers = game.headers
    board_start = board.copy()
    meta = {"player": _player_side(headers),
            "white": headers.get("White", "White"),
            "black": headers.get("Black", "Black"),
            "result": headers.get("Result", "*"),
            "date": headers.get("Date", ""),
            "event": headers.get("Event", ""),
            "site": headers.get("Site", ""),
            "eco": headers.get("ECO", ""),
            "plies": len(moves)}

    entries = []
    ucis = []
    openings = []
    pos = eng.analyse(board.fen(), depth, multipv=1)   # evaluation of the start
    prev_eval = pos[0]["cp"] if pos else 0
    prev_mate = pos[0]["mate"] if pos else None

    for i, mv in enumerate(moves):
        if progress:
            progress(i, len(moves))
        turn = board.turn
        # pass 1: the verdict (full depth, single line); pass 2: alternatives
        before = eng.analyse(board.fen(), depth, multipv=1)
        alts = eng.analyse(board.fen(), max(10, depth - 3), multipv=multipv)
        best = before[0] if before else (alts[0] if alts else
                                        {"cp": prev_eval, "mate": prev_mate,
                                         "pv": [], "multipv": 1})
        fen_before = board.fen()
        mat_before = _material(board)
        rep_before = CP.report(board)
        gives_check = board.gives_check(mv)
        is_capture = board.is_capture(mv)
        is_castle = board.is_castling(mv)
        promo = mv.promotion
        san = board.san(mv)
        forced = board.legal_moves.count() == 1
        facts = [dict(kind=k, **slots) for k, slots in CF.move_facts(board, mv)]
        mover_mate_before = best["mate"] if turn == chess.WHITE else (
            None if best.get("mate") is None else -best["mate"])
        best_line = _pv_line(board, best.get("pv"), turn, mover_mate_before)
        ucis.append(mv.uci())
        m = OP.match_opening(ucis)
        if m and m[1] == len(ucis):
            openings.append({"ply": i + 1, "name": m[0],
                             "color": "white" if turn == chess.WHITE
                             else "black", "san": san})
        cand = []
        for c in alts[:3]:
            if c.get("pv"):
                try:
                    m2 = chess.Move.from_uci(c["pv"][0])
                    cand.append({"uci": c["pv"][0], "san": board.san(m2),
                                 "cp": c["cp"], "mate": c["mate"]})
                except (ValueError, AssertionError):
                    pass
        best_san = cand[0]["san"] if cand else san
        best_uci = cand[0]["uci"] if cand else mv.uci()
        played_is_best = bool(best.get("pv")) and mv.uci() == best["pv"][0]
        board.push(mv)
        if board.is_checkmate():
            # the engine returns 'bestmove (none)' with no info on a mated
            # position - record the mate explicitly
            sgn = 1 if turn == chess.WHITE else -1
            after = [{"cp": (MATE_CP - 100) * sgn, "mate": sgn, "pv": [],
                      "multipv": 1, "wdl": (1000 * sgn, 0, 0)}]
        else:
            after = eng.analyse(board.fen(), depth, multipv=1)
        cur = after[0] if after else best
        mat_after = _material(board)
        concepts = _concept_deltas(rep_before, CP.report(board), turn)

        best_cp = best["cp"]
        cur_cp = cur["cp"]
        mover_before = best_cp if turn == chess.WHITE else -best_cp
        mover_after = cur_cp if turn == chess.WHITE else -cur_cp
        loss = max(0, int(mover_before - mover_after))
        if played_is_best:
            # the mover played the engine's own first choice: whatever the
            # depth-limited eval wobble says, this cannot be a mistake
            loss = min(loss, 25)
        mate_after = cur["mate"] if turn == chess.WHITE else (
            None if cur["mate"] is None else -cur["mate"])
        mate_before = best["mate"] if turn == chess.WHITE else (
            None if best["mate"] is None else -best["mate"])

        gap = 0
        if len(before) > 1:
            gap = before[0]["cp"] - before[1]["cp"]

        # quality (mate distances compared in the MOVER's own frame)
        if (loss <= 30 and mate_after is not None and mate_after > 0
                and (board.is_checkmate() or mate_before is None
                     or mate_before > mate_after)):
            quality = "brilliant"
        elif loss <= 30 and gap >= T_GREAT_GAP and (
                is_capture or gives_check or mover_after - mover_before >= 50):
            quality = "great"
        elif loss <= 15 and best["pv"] and mv.uci() == best["pv"][0]:
            quality = "best"
        elif loss <= T_INACCURACY:
            quality = "good"
        elif loss <= T_MISTAKE:
            quality = "inaccuracy"
        elif loss <= T_BLUNDER:
            quality = "mistake"
        else:
            quality = "blunder"

        entries.append({
            "ply": i + 1, "no": i // 2 + 1,
            "color": "white" if turn == chess.WHITE else "black",
            "san": san, "uci": mv.uci(), "fen_before": fen_before,
            "eval_cp": cur_cp, "eval_mate": mate_after,
            "eval_before_cp": best_cp, "eval_before_mate": best["mate"],
            "best": best_uci,
            "best_san": best_san,
            "best_cp": best_cp,
            "candidates": cand,
            "pv": " ".join(best.get("pv", [])[:6]),
            "loss_cp": loss, "quality": quality,
            "check": bool(gives_check), "capture": bool(is_capture),
            "castle": bool(is_castle), "promotion": promo or 0,
            "mate_after": mate_after,
            "material_swing": mat_after - mat_before,
            "facts": facts,
            "concepts": concepts,
            "best_line": best_line,
            "best_impact": _impact_of(best_line),
            "wdl": cur.get("wdl"),
            "wdl_before": best.get("wdl"),
            "forced": bool(forced),
            "quiet": (not is_capture and not gives_check and loss <= 30
                      and quality in ("good", "best")),
        })
        prev_eval, prev_mate = cur_cp, mate_after

    if progress:
        progress(len(moves), len(moves))

    # derived story beats
    turning = [e["ply"] for e in entries
               if e["loss_cp"] >= T_TURN or e["quality"] in ("mistake",
                                                             "blunder")
               or (e["mate_after"] and e["mate_after"] > 0)]
    decisions = [e["ply"] for e in entries
                 if len(e["candidates"]) >= 2
                 and 0 <= e["candidates"][0]["cp"] - e["candidates"][1]["cp"]
                 <= 120]
    quiet = []
    run = []
    for e in entries:
        if e["quiet"]:
            run.append(e["ply"])
        else:
            if len(run) >= 3:
                quiet.append([run[0], run[-1]])
            run = []
    if len(run) >= 3:
        quiet.append([run[0], run[-1]])

    out = {"meta": meta, "moves": entries, "turning_points": turning,
           "decision_points": decisions[:6], "quiet_runs": quiet,
           "openings": openings, "phases": _phase_markers(board_start, entries),
           "eval_words": _eval_words}
    if own:
        eng.close()
    return out


def analysis_to_jsonable(analysis):
    """Strip callables for caching (eval_words is rebuilt by narrative)."""
    out = dict(analysis)
    out.pop("eval_words", None)
    return out


def analyze_pgn(path, depth=13, multipv=3, engine=None, progress=None):
    return analyze_game(load_pgn(path), depth=depth, multipv=multipv,
                        engine=engine, progress=progress)


def cache_path():
    return os.path.join(ROOT, "data", "analysis_last.json")


def save_analysis(analysis, path=None):
    path = path or cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(analysis_to_jsonable(analysis), f, indent=1)
    return path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="analyze a PGN with Stockfish")
    ap.add_argument("pgn")
    ap.add_argument("--depth", type=int, default=13)
    ap.add_argument("--multipv", type=int, default=3)
    a = ap.parse_args()
    res = analyze_pgn(a.pgn, depth=a.depth, multipv=a.multipv,
                      progress=lambda i, n: print("  %d/%d" % (i, n),
                                                  end="\r"))
    print()
    for e in res["moves"]:
        print("%3d %-8s %-8s loss=%4d  %-11s %s" % (
            e["no"], e["color"][:5], e["san"], e["loss_cp"], e["quality"],
            e["best_san"]))
    print("turning points:", res["turning_points"])
    print("saved:", save_analysis(res))
