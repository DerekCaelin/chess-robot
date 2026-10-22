# chessapp/chesspos.py - positional advantage, computed from the board (the
# engine only gives one number; these are the measurable claims behind
# "White controls the center" or "Black's king sits behind a solid shield").
import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5,
          chess.QUEEN: 9, chess.KING: 0}

HOME = {
    chess.WHITE: {chess.KNIGHT: (chess.B1, chess.G1),
                  chess.BISHOP: (chess.C1, chess.F1)},
    chess.BLACK: {chess.KNIGHT: (chess.B8, chess.G8),
                  chess.BISHOP: (chess.C8, chess.F8)},
}

CENTER = (chess.D4, chess.E4, chess.D5, chess.E5)


def material(board):
    """White minus black, in pawns."""
    total = 0
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p:
            total += VALUES[p.piece_type] * (1 if p.color == chess.WHITE
                                             else -1)
    return total


def _count(board, color, types):
    return sum(1 for sq in chess.SQUARES
               if (lambda p: p is not None and p.color == color
                   and p.piece_type in types)(board.piece_at(sq)))


def center_control(board):
    """(white, black) attacks on the four central squares."""
    w = b = 0
    for sq in CENTER:
        w += len(board.attackers(chess.WHITE, sq))
        b += len(board.attackers(chess.BLACK, sq))
    return w, b


def king_safety(board, color):
    """Rough shield score: pawns in front of the king minus open files."""
    ksq = board.king(color)
    if ksq is None:
        return 0
    kf = chess.square_file(ksq)
    fwd = 1 if color == chess.WHITE else -1
    score = 0
    for f in (kf - 1, kf, kf + 1):
        if not (0 <= f < 8):
            continue
        r = chess.square_rank(ksq) + fwd
        if 0 <= r < 8:
            p = board.piece_at(chess.square(f, r))
            if p is not None and p.piece_type == chess.PAWN \
                    and p.color == color:
                score += 1
        if not any(board.piece_at(chess.square(f, rr)) is not None
                   and board.piece_at(chess.square(f, rr)).piece_type
                   == chess.PAWN for rr in range(8)):
            score -= 1
    return score


def development(board, color):
    """Minor pieces off their home squares + 2 for having castled."""
    n = 0
    homes = HOME[color]
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if (p is not None and p.color == color
                and p.piece_type in (chess.KNIGHT, chess.BISHOP)
                and sq not in homes.get(p.piece_type, ())):
            n += 1
    if board.king(color) in (chess.G1, chess.C1, chess.G8, chess.C8):
        n += 2
    return n


def space(board, color):
    """Squares attacked in the opponent's half."""
    rank_lo, rank_hi = (4, 8) if color == chess.WHITE else (0, 4)
    n = 0
    for r in range(rank_lo, rank_hi):
        for f in range(8):
            if board.attackers(color, chess.square(f, r)):
                n += 1
    return n


def structure(board, color):
    """(passed, isolated, doubled) pawn counts."""
    passed = isolated = doubled = 0
    files = {f: 0 for f in range(8)}
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p is None or p.piece_type != chess.PAWN or p.color != color:
            continue
        files[chess.square_file(sq)] += 1
        f = chess.square_file(sq)
        r = chess.square_rank(sq)
        fwd = 1 if color == chess.WHITE else -1
        enemy = not color
        ahead = False
        for ff in (f - 1, f, f + 1):
            if not (0 <= ff < 8):
                continue
            for rr in range(8):
                q = board.piece_at(chess.square(ff, rr))
                if (q is not None and q.piece_type == chess.PAWN
                        and q.color == enemy
                        and (rr - r) * fwd > 0):
                    ahead = True
        if not ahead:
            passed += 1
    for f in range(8):
        if files[f] > 1:
            doubled += files[f] - 1
        if files[f] and not any(files[g] for g in (f - 1, f + 1)
                                if 0 <= g < 8):
            isolated += files[f]
    return passed, isolated, doubled


def bishop_pair(board, color):
    return _count(board, color, (chess.BISHOP,)) >= 2


def rook_files(board, color):
    """Rooks on files with no friendly pawn."""
    n = 0
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p is None or p.color != color or p.piece_type != chess.ROOK:
            continue
        f = chess.square_file(sq)
        if not any(board.piece_at(chess.square(f, r)) is not None
                   and board.piece_at(chess.square(f, r)).piece_type
                   == chess.PAWN
                   and board.piece_at(chess.square(f, r)).color == color
                   for r in range(8)):
            n += 1
    return n


def outposts(board, color):
    """Knights on protected squares in the opponent's half."""
    n = 0
    rank_lo, rank_hi = (4, 8) if color == chess.WHITE else (0, 4)
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if (p is not None and p.color == color
                and p.piece_type == chess.KNIGHT
                and rank_lo <= chess.square_rank(sq) < rank_hi
                and board.attackers(color, sq)):
            n += 1
    return n


def report(board):
    """Everything measurable about the position, both sides."""
    wc, bc = center_control(board)
    return {
        "material": material(board),
        "center": (wc, bc),
        "king_safety": (king_safety(board, chess.WHITE),
                        king_safety(board, chess.BLACK)),
        "development": (development(board, chess.WHITE),
                        development(board, chess.BLACK)),
        "space": (space(board, chess.WHITE), space(board, chess.BLACK)),
        "structure": (structure(board, chess.WHITE),
                      structure(board, chess.BLACK)),
        "bishop_pair": (bishop_pair(board, chess.WHITE),
                        bishop_pair(board, chess.BLACK)),
        "rook_files": (rook_files(board, chess.WHITE),
                       rook_files(board, chess.BLACK)),
        "outposts": (outposts(board, chess.WHITE),
                     outposts(board, chess.BLACK)),
    }
