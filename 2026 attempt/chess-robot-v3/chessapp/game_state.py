# game_state.py - persisted chess game (every move is saved as it happens).
#
# data/game.json schema:
#   {"moves": ["e2e4", "e7e5", ...],   # UCI strings, human then robot
#    "player_color": "white",          # which side the human plays
#    "level": 5,                        # engine level the game started on
#    "started": "2026-09-16T21:00",     # ISO timestamp
#    "result": null | "1-0" | ...}      # set when finished

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "game.json")


def exists():
    return os.path.exists(PATH)


def save(moves, player_color, level, result=None, started=None, stars=None):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    data = {
        "moves": list(moves),
        "player_color": player_color,
        "level": level,
        "result": result,
        "started": started or time.strftime("%Y-%m-%dT%H:%M:%S"),
        # moves the PLAYER starred during the game (1-based ply) - shown
        # again in the analysis review (2026-10-24)
        "stars": sorted(set(stars or [])),
    }
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)
    return data


def load():
    try:
        with open(PATH) as f:
            data = json.load(f)
        if isinstance(data.get("moves"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


LAST_PATH = os.path.join(ROOT, "data", "last_game.json")


def save_last(data=None):
    """Snapshot the finished game for the Analysis module. The analysis works
    off THIS file, so starting a new game never destroys it."""
    data = data or load()
    if not data or not data.get("moves"):
        return None
    os.makedirs(os.path.dirname(LAST_PATH), exist_ok=True)
    with open(LAST_PATH, "w") as f:
        json.dump(data, f, indent=1)
    # Adaptive Level: a game that ends in checkmate updates the strength
    # estimate and re-picks the level (2026-10-24)
    try:
        from chessapp import levels
        levels.adapt_after_game(data)
    except Exception:
        pass
    return data


def load_last():
    try:
        with open(LAST_PATH) as f:
            data = json.load(f)
        if isinstance(data.get("moves"), list) and data["moves"]:
            return data
    except (OSError, ValueError):
        pass
    return None


def clear():
    try:
        os.remove(PATH)
    except OSError:
        pass


def summary():
    """Short human text for the Home screen ('White to play, 6 moves' ...)."""
    d = load()
    if not d:
        return None
    n = len(d["moves"])
    if d.get("result"):
        return "Finished game (%d moves)" % n
    side = "You" if (n % 2 == 0) == (d.get("player_color") == "white") else "Robot"
    return "%d moves - %s to play" % (n, side)


def sans_of(ucis):
    """The moves as readable notation WITH check and checkmate (2026-11-04:
    the in-game list showed raw UCI - 'e2e4', never 'Bxb5+')."""
    import chess
    out = []
    b = chess.Board()
    for u in ucis or []:
        try:
            mv = chess.Move.from_uci(u)
            out.append(b.san(mv))
            b.push(mv)
        except Exception:
            out.append(u)
    return out
