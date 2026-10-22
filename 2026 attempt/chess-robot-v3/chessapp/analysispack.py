# chessapp/analysispack.py - the Analysis module's data pipeline: take the
# LAST FINISHED game and prepare per-move text + audio, staged in the
# background, cached under data/analysis/<game-hash>/ until the game changes.
#
# Stage 1: the engine pass + the short text for EVERY move.
# Stage 2: one sound file per move (starting with the first, so move 1 can
#          play while the rest are still synthesizing).
import hashlib
import json
import os
import shutil

import chess
import chess.pgn

from chessapp import analysis as AN
from chessapp import audio
from chessapp import game_state
from chessapp import narrative as NR
from chessapp import voice as VO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "analysis")
VOICES = ("none", "espeak", "piper")
VOICE_LABELS = {"none": "None (fastest)", "espeak": "espeak (fast)",
                "piper": "piper (slow)"}


def game_hash(game):
    return hashlib.sha1(json.dumps(game, sort_keys=True).encode()
                        ).hexdigest()[:12]


def game_from_state(data):
    """The saved game as a chess.pgn.Game, with names that mark the player
    (the narrator takes the player's side from the game itself)."""
    player = (data.get("player_color") or "white").lower()
    g = chess.pgn.Game()
    g.headers["White"] = "Player" if player == "white" else "Robot"
    g.headers["Black"] = "Player" if player == "black" else "Robot"
    g.headers["Date"] = (data.get("started") or "")[:10].replace("-", ".")
    g.headers["Event"] = "Chess Robot"
    node = g
    board = g.board()
    for u in data.get("moves", []):
        mv = chess.Move.from_uci(u)
        node = node.add_variation(mv)
        board.push(mv)
    res = data.get("result")
    if not res:
        res = ("0-1" if board.turn == chess.WHITE else "1-0") \
            if board.is_checkmate() else "*"
    g.headers["Result"] = res
    return g


def winner_of(game):
    res = game.headers.get("Result", "*")
    return ("white" if res == "1-0" else "black" if res == "0-1" else None)


class AnalysisPack:
    """One finished game, prepared for the Analysis screens."""

    def __init__(self, game):
        self.game = game
        self.hash = game_hash(game)
        self.dir = os.path.join(CACHE, self.hash)
        self.moves = []
        self.analysis = None
        self._run_id = 0

    def cancel(self):
        """Stop the current generation run. Files already written are kept -
        switching voices never throws away finished work."""
        self._run_id += 1

    def text_path(self):
        return os.path.join(self.dir, "text.json")

    def wav_path(self, ply, voice):
        return os.path.join(self.dir, "m%03d_%s.wav" % (ply, voice))

    def has_text(self):
        return os.path.exists(self.text_path())

    def move_count(self):
        return len(self.game.get("moves", []))

    def board_after(self, upto):
        """ChessBoard layout after `upto` plies (for the board widget)."""
        from ChessBoard import ChessBoard
        b = ChessBoard()
        for u in self.game.get("moves", [])[:upto]:
            b.addTextMove(u)
        return b

    def generate(self, voice="none", on_progress=None, depth=13):
        """Staged background generation. on_progress(done, total, stage).
        Cancellable between moves; one bad move never kills the run."""
        token = self._run_id          # cancel() invalidates this run
        os.makedirs(self.dir, exist_ok=True)
        if not self.has_text():
            g = game_from_state(self.game)
            self.analysis = AN.analyze_game(g, depth=depth)
            self.moves = NR.per_move(self.analysis)
            with open(self.text_path(), "w") as f:
                json.dump({"moves": self.moves,
                           "analysis": AN.analysis_to_jsonable(self.analysis)},
                          f, indent=1)
        else:
            self._load_text()
        if on_progress:
            on_progress(0, max(1, len(self.moves)), "text")
        if voice in ("espeak", "piper"):
            total = max(1, len(self.moves))
            for i, m in enumerate(self.moves):
                if self._run_id != token:
                    break
                out = self.wav_path(m["ply"], voice)
                if not os.path.exists(out):
                    speech = (m.get("speech") or "").strip()
                    if not speech:
                        continue
                    try:
                        if voice == "espeak":
                            VO.synth_espeak(speech, out)
                        else:
                            VO.synth_piper(speech, out,
                                           model=VO.PIPER_MODELS["alba"])
                    except Exception as e:
                        # one bad line must not stop the rest of the game
                        print("voice: move %d failed: %s" % (m["ply"], e))
                        continue
                if on_progress:
                    on_progress(i + 1, total, "audio")
        return self.moves

    def _load_text(self):
        try:
            with open(self.text_path()) as f:
                data = json.load(f)
            self.moves = data.get("moves", [])
            self.analysis = data.get("analysis")
        except (OSError, ValueError):
            self.moves = []

    def ready(self, ply, voice):
        """The sound file for this move exists (voice off = always ready)."""
        return voice == "none" or os.path.exists(self.wav_path(ply, voice))

    def sound_len(self, ply, voice):
        if voice == "none":
            return 0.0
        return audio.wav_duration(self.wav_path(ply, voice))


def auto_advance_seconds(words, sound_len):
    """Auto-advance timer: the sound plus 2 s, or - with no voice - scaled to
    the text, never under 5 s nor over 10 s."""
    if sound_len and sound_len > 0:
        return sound_len + 2.0
    return max(5.0, min(10.0, (words or 1) / 2.5))


def load_pack():
    """The last finished game's pack; caches of EARLIER games are discarded
    ('throw out any analysis or sound leftover from the previous analysis')."""
    data = game_state.load_last()
    if not data:
        return None
    pk = AnalysisPack(data)
    if os.path.isdir(CACHE):
        for name in os.listdir(CACHE):
            if name != pk.hash:
                shutil.rmtree(os.path.join(CACHE, name), ignore_errors=True)
    return pk
