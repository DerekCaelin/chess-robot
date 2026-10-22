# chessapp/chessfacts.py - what a move DOES to the position, in computable
# facts: captures, checks, hanging pieces, opened lines, forks, pins, pawn
# structure, castling. The narrator turns these into commentary; the engine
# only supplies evals and lines, this is pure board analysis.
import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5,
          chess.QUEEN: 9, chess.KING: 0}

PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight",
               chess.BISHOP: "bishop", chess.ROOK: "rook",
               chess.QUEEN: "queen", chess.KING: "king"}


def piece_name(piece):
    return PIECE_NAMES.get(piece.piece_type, "piece")


def color_name(color):
    return "White" if color == chess.WHITE else "Black"


def _attacker_name(board, sq):
    p = board.piece_at(sq)
    return piece_name(p) if p else "piece"


def _is_hanging(board, sq, owner):
    """True when the piece on sq is attacked and cannot safely stand there:
    undefended, or attacked by something worth less than it."""
    piece = board.piece_at(sq)
    if piece is None or piece.color != owner or piece.piece_type == chess.KING:
        return False
    attackers = board.attackers(not owner, sq)
    if not attackers:
        return False
    defenders = board.attackers(owner, sq)
    if not defenders:
        return True
    cheapest_att = min(VALUES[board.piece_at(a).piece_type] for a in attackers)
    return cheapest_att < VALUES[piece.piece_type]


def _line_has_pawn(board, squares):
    return any(board.piece_at(s) is not None
               and board.piece_at(s).piece_type == chess.PAWN for s in squares)


def _ray(board, frm, direction, steps=8):
    """Squares along a direction from frm (file/rank/diagonal)."""
    out = []
    f, r = chess.square_file(frm), chess.square_rank(frm)
    df, dr = direction
    for i in range(1, steps + 1):
        nf, nr = f + df * i, r + dr * i
        if 0 <= nf < 8 and 0 <= nr < 8:
            out.append(chess.square(nf, nr))
        else:
            break
    return out


def move_facts(board, move):
    """Structured facts about `move` in position `board` (BEFORE the move).
    Every fact is strictly true - computed, never guessed."""
    facts = []
    mover = board.turn
    foe = not mover
    after = board.copy()
    after.push(move)

    # captures / special moves
    if board.is_en_passant(move):
        facts.append(("capture", {"piece": "pawn", "what": "a pawn",
                                  "square": chess.square_name(move.to_square),
                                  "ep": True}))
    elif board.is_capture(move):
        victim = board.piece_at(move.to_square)
        facts.append(("capture", {"piece": piece_name(victim),
                                  "what": ("the " + piece_name(victim)),
                                  "square": chess.square_name(move.to_square),
                                  "value": VALUES[victim.piece_type]}))
    if move.promotion:
        facts.append(("promotion", {"piece": PIECE_NAMES.get(move.promotion,
                                                             "queen"),
                                    "square": chess.square_name(
                                        move.to_square)}))
    if board.is_castling(move):
        side = "kingside" if chess.square_file(move.to_square) == 6 else \
            "queenside"
        facts.append(("castles", {"side": side}))

    # check / mate / double check
    if after.is_checkmate():
        facts.append(("mate", {}))
    elif board.gives_check(move):
        king_sq = after.king(foe)
        attackers = after.attackers(mover, king_sq)
        facts.append(("double_check" if len(attackers) >= 2 else "check",
                      {"piece": piece_name(board.piece_at(move.from_square))}))

    # what the move leaves exposed (the mover's own pieces)
    worst = None
    for sq in chess.SQUARES:
        piece = after.piece_at(sq)
        if piece is None or piece.color != mover:
            continue
        if _is_hanging(after, sq, mover) and not _is_hanging(board, sq, mover):
            cand = {"piece": piece_name(piece),
                    "square": chess.square_name(sq),
                    "attacker": _attacker_name(
                        after, min(after.attackers(foe, sq),
                                   key=lambda a: VALUES[after.piece_at(
                                       a).piece_type])),
                    "value": VALUES[piece.piece_type]}
            if worst is None or cand["value"] > worst["value"]:
                worst = cand
    if worst:
        facts.append(("exposes", worst))

    # what the moved piece now threatens
    mover_piece = after.piece_at(move.to_square)
    if mover_piece is not None:
        targets = []
        for sq in after.attacks(move.to_square):
            t = after.piece_at(sq)
            if (t is not None and t.color == foe
                    and t.piece_type != chess.KING
                    and _is_hanging(after, sq, foe)):
                targets.append((VALUES[t.piece_type],
                                {"piece": piece_name(t),
                                 "square": chess.square_name(sq)}))
        if targets:
            facts.append(("threatens", max(targets, key=lambda tv: tv[0])[1]))

        # fork: the moved piece attacks two worthwhile pieces at once
        hits = [sq for sq in after.attacks(move.to_square)
                if after.piece_at(sq) is not None
                and after.piece_at(sq).color == foe
                and VALUES[after.piece_at(sq).piece_type] >= 3]
        king_hit = after.king(foe) in after.attacks(move.to_square)
        if len(hits) + (1 if king_hit else 0) >= 2:
            a = after.piece_at(hits[0]) if hits else None
            b = after.piece_at(hits[1]) if len(hits) > 1 else None
            names = ([piece_name(x) for x in (a, b) if x is not None]
                     + (["king"] if king_hit else []))
            second = names[1] if names[1] != names[0] else "the other " + \
                names[0]
            facts.append(("fork", {"first": names[0], "second": second,
                                   "piece": piece_name(mover_piece)}))

        # pin: a slider looking through a piece onto a worthier one
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1),
                          (1, 1), (1, -1), (-1, 1), (-1, -1)):
            ray = _ray(after, move.to_square, direction)
            seen = [sq for sq in ray if after.piece_at(sq) is not None]
            if len(seen) >= 2:
                first, second = seen[0], seen[1]
                p1, p2 = after.piece_at(first), after.piece_at(second)
                if (p1.color == foe and p2.color == foe
                        and p1.piece_type != chess.KING
                        and VALUES[p2.piece_type] > VALUES[p1.piece_type]
                        and mover_piece.piece_type in (chess.BISHOP,
                                                       chess.ROOK,
                                                       chess.QUEEN)):
                    facts.append(("pin", {"piece": piece_name(p1),
                                          "square": chess.square_name(first),
                                          "target": piece_name(p2)}))
                    break

    # lines opened for friendly rooks/bishops/queens
    if (board.piece_at(move.from_square) is not None
            and board.piece_at(move.from_square).piece_type == chess.PAWN):
        frm_file = chess.square_file(move.from_square)
        for sq in after.pieces(chess.ROOK, mover) | \
                after.pieces(chess.QUEEN, mover):
            if chess.square_file(sq) == frm_file:
                col = [chess.square(frm_file, r) for r in range(8)]
                if _line_has_pawn(board, col) and not _line_has_pawn(after,
                                                                     col):
                    facts.append(("opens_line", {
                        "piece": piece_name(after.piece_at(sq)),
                        "line": chess.FILE_NAMES[frm_file] + "-file"}))
                    break
        for sq in after.pieces(chess.BISHOP, mover) | \
                after.pieces(chess.QUEEN, mover):
            for direction in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                ray = _ray(after, sq, direction)
                if move.to_square in ray or move.from_square in ray:
                    if _line_has_pawn(board, ray) and not _line_has_pawn(
                            after, ray):
                        facts.append(("opens_line", {
                            "piece": piece_name(after.piece_at(sq)),
                            "line": "long diagonal"}))
                        break
            else:
                continue
            break

    # pawn structure changes
    if (board.piece_at(move.from_square) is not None
            and board.piece_at(move.from_square).piece_type == chess.PAWN):
        to = move.to_square
        tf = chess.square_file(to)
        fwd = 1 if mover == chess.WHITE else -1

        def pawns_on(color, f):
            if not (0 <= f < 8):
                return []
            out = []
            for r in range(8):
                p = after.piece_at(chess.square(f, r))
                if p is not None and p.piece_type == chess.PAWN \
                        and p.color == color:
                    out.append(chess.square(f, r))
            return out

        # passed: no enemy pawn ahead on its own or the adjacent files
        enemy_ahead = False
        for f in (tf - 1, tf, tf + 1):
            for sq in pawns_on(foe, f):
                if (chess.square_rank(sq) - chess.square_rank(to)) * fwd > 0:
                    enemy_ahead = True
                    break
            if enemy_ahead:
                break
        if not enemy_ahead:
            facts.append(("passed", {"square": chess.square_name(to)}))
        if not pawns_on(mover, tf - 1) and not pawns_on(mover, tf + 1):
            facts.append(("isolated", {"square": chess.square_name(to)}))

    return facts
