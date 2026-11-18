# levels.py - the Stockfish level table and the Adaptive Level tracker
# (2026-10-24).
#
# The levels ARE Elo values: the engine runs with UCI_LimitStrength and
# UCI_Elo, so the number shown to the player is the strength actually set.
#
# ADAPTIVE LEVEL: when it is on, a game that ENDS IN CHECKMATE updates an
# estimate of the player's strength and re-picks the level from it. The
# method is a small Elo tracker:
#
#     S = 1.0  the player delivered mate, 0.0  the player got mated
#     E = 1 / (1 + 10 ** ((level_elo - player_elo) / 400))   (expected)
#     player_elo += K * (S - E)                              (K = 24)
#     target = player_elo + 50        # the engine keeps a slight edge:
#                                     # challenging, but feasible
#
# Guards: one level per game (no oscillation), clamped 1..10, and ONLY
# games ending in checkmate count - a draw or an abandoned game is weak
# evidence.

from chessapp import settings

ELOS = [450, 600, 750, 900, 1050, 1200, 1400, 1650, 1950, 2400]
K = 24
TARGET_EDGE = 50          # the engine stays this much above the estimate


def level_elo(level):
    level = max(1, int(level))
    return ELOS[min(level, len(ELOS)) - 1]


def elo_for_level(target):
    """The level whose Elo is nearest the target (clamped 1..len)."""
    best, bestd = 1, abs(ELOS[0] - target)
    for i, e in enumerate(ELOS):
        d = abs(e - target)
        if d < bestd:
            best, bestd = i + 1, d
    return best


def adapt_after_game(game, force=False):
    """One Adaptive Level update per game. `game` is the saved game dict.
    Returns the new level, or None when nothing changed.  Default OFF
    (2026-11-08): the feature is unused and its UI switch is gone - it only
    runs when data/settings.json turns it on explicitly."""
    if not settings.get("stockfish.adaptive", False):
        return None
    moves = game.get("moves") or []
    if not moves:
        return None
    # once per game: the started timestamp identifies it (keep a small
    # history, so replaying an older game never double-counts)
    key = game.get("started") or ",".join(moves[:4])
    done = [k for k in (settings.get("stockfish.adapted_games") or [])
            if k]
    if not force and key in done:
        return None
    # ONLY games that end in checkmate are evidence
    import chess
    b = chess.Board()
    try:
        for u in moves:
            b.push_uci(u)
    except ValueError:
        return None
    if not b.is_checkmate():
        return None
    # who delivered it?  the side that made the last move
    winner = "white" if b.turn == chess.BLACK else "black"
    player = (game.get("player_color") or "white").lower()
    s = 1.0 if winner == player else 0.0
    level = int(settings.get("stockfish.level", 5))
    p = float(settings.get("stockfish.player_elo", level_elo(level)))
    e = 1.0 / (1.0 + 10.0 ** ((level_elo(level) - p) / 400.0))
    p += K * (s - e)
    settings.set("stockfish.player_elo", p)
    settings.set("stockfish.adapted_games", (done + [key])[-20:])
    # a game ending in CHECKMATE adjusts the level in the winner's direction
    # (the user's rule) - one step, so win/lose settles near 50% - while the
    # Elo estimate above is the long-run picture of the player
    new = level + (1 if s > 0.5 else -1)
    new = max(1, min(len(ELOS), new))
    if new != level:
        settings.set("stockfish.level", new)
    return new
