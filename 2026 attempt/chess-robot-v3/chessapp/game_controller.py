# game_controller.py - the chess game loop, stripped of console I/O.
#
# Extracted from CBint.py so the tkinter UI and the CLI wrapper can share one
# implementation. Responsibilities:
#   * Stockfish subprocess lifecycle (UCI over pipes)
#   * move validation via ChessBoard
#   * asking the vision layer for the human move (blocking; run in a thread)
#   * executing the robot's reply physically (LEGO_Chess_rpd), lazily imported
#   * persisting every move through game_state
#
# Callbacks (all optional, called from the thread that drives the methods):
#   on_speak(text)        anything the robot says
#   on_status(text)       transient status lines for the UI / console
#   on_board(board, moves) board state changed -> redraw

import subprocess
import sys
import time

from ChessBoard import ChessBoard
from chessapp import settings, game_state
import CBstate


class ChessGame:
    REASONS = (
        "No reason", "Invalid move", "Invalid colour", "Invalid 'from' location",
        "Invalid 'to' location", "Must set promotion", "Game is over",
        "Ambiguous move")

    def __init__(self, on_speak=None, on_status=None, on_board=None,
                 on_player_move=None, on_computer_move=None,
                 use_robot=True):
        self.on_speak = on_speak
        self.on_status = on_status
        self.on_board = on_board
        self.on_player_move = on_player_move
        self.on_computer_move = on_computer_move
        self.use_robot = use_robot
        self.cb = ChessBoard()
        self.cb.setPromotion(self.cb.QUEEN)
        self.engine = None
        self.fmove = ""
        self.movelist = []
        self.game_over = False
        self.result_text = None

    # ── small plumbing ───────────────────────────────────────────────────────

    def _speak(self, text):
        if self.on_speak:
            self.on_speak(text)
        else:
            from chessapp import audio
            audio.speak(text)

    def _status(self, text):
        if self.on_status:
            self.on_status(text)

    def _notify_board(self):
        if self.on_board:
            self.on_board(self.cb.getBoard(), list(self.movelist))

    @staticmethod
    def _RD():
        import LEGO_Chess_rpd
        return LEGO_Chess_rpd

    def _put(self, command):
        self.engine.stdin.write(command + "\n")
        self.engine.stdin.flush()

    def _get(self, until="readyok"):
        """Drain engine stdout until `until` (or bestmove) arrives."""
        while True:
            text = self.engine.stdout.readline().strip()
            if not text:
                continue
            if text == until or text.startswith("bestmove"):
                return text

    def _sget(self):
        while True:
            text = self.engine.stdout.readline().strip()
            if text.startswith("bestmove"):
                return text

    # ── engine / game lifecycle ──────────────────────────────────────────────

    def start_engine(self):
        if self.engine and self.engine.poll() is None:
            return True
        self.engine = subprocess.Popen(
            [CBstate.chessenginepath],
            universal_newlines=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        self._put("uci")
        self._get("uciok")
        return True

    def shutdown_engine(self):
        try:
            self._put("quit")
            self.engine.wait(timeout=3)
        except Exception:
            pass

    def new_game(self, resume=False):
        """Fresh start, or resume from data/game.json if asked and present."""
        self.start_engine()
        self.cb.resetBoard()
        if self.use_robot:               # captured pieces start back at slot 1
            try:
                self._RD().reset_graveyard()
            except Exception:
                pass
        self.fmove = ""
        self.movelist = []
        self.game_over = False
        self.result_text = None
        self._put("ucinewgame")
        self._put("isready")
        self._get("readyok")
        self._apply_engine_level()
        if resume:
            saved = game_state.load()
            if saved:
                for mv in saved["moves"]:
                    if self.cb.addTextMove(mv) is False:
                        break
                    self.fmove = (self.fmove + " " + mv).strip()
                    self.movelist.append(mv)
                settings.set("player.color",
                             saved.get("player_color", "white"))
        level = settings.get("stockfish.level", 5)
        color = settings.get("player.color", "white")
        game_state.save(self.movelist, color, level, result=None)
        self._notify_board()
        return {"ok": True, "resumed": bool(resume and self.movelist)}

    def _apply_engine_level(self):
        self._put("setoption name Slow Mover value " + CBstate.slowmover)
        level = max(1, int(settings.get("stockfish.level", 5)))
        elos = settings.get("stockfish.levels_elo",
                            [450, 600, 750, 900, 1050, 1200, 1400, 1650,
                             1950, 2400])
        elo = elos[min(level, len(elos)) - 1]
        self._put("setoption name UCI_LimitStrength value true")
        self._put("setoption name UCI_Elo value %d" % elo)
        self._put("isready")
        self._get("readyok")

    # ── player-move acquisition (blocking; call from worker thread) ─────────

    def wait_player_move(self, stop=None, status=None, on_ready=None):
        """Blocking: hands-free vision detection of the human's move.
        Returns the raw bmessage: '' / 'm' + 4 chars / 'JOSTLE'.
        stop: optional callable - returns '' early when it flips true (UI
        abandoned the turn); the camera is released either way."""
        import playermove_rpd as RDpm
        valid = self.cb.getValidMoves((4, 7))
        if stop is not None and stop():
            return ""
        return RDpm.getplayermove(self.cb.getBoard(), valid, stop=stop,
                                  status=status, on_ready=on_ready)

    def handle_jostle(self):
        """Board/camera moved. Try automatic mid-game re-detection."""
        import piece_detect
        self._status("Board moved - recalibrating automatically...")
        if self.use_robot:
            try:
                RD = self._RD()
                if RD.sp is not None:
                    RD.park()          # get the arm out of the camera's
            except Exception:          # view before re-locking corners
                pass
        return piece_detect.recover_calibration()

    # ── one full turn ────────────────────────────────────────────────────────

    def submit_player_move(self, bmessage):
        """Validate the human move, ask Stockfish, physically play the reply.
        Returns dict(accepted, error, player_move, computer_move, check,
        game_over, result_text)."""
        board_before = self.cb.getBoard()
        brdmove = bmessage[1:5].lower()
        out = {"accepted": False, "error": None, "player_move": brdmove,
               "computer_move": None, "check": False,
               "game_over": False, "result_text": None}
        if not brdmove.strip():
            out["error"] = "no move detected"
            return out
        if self.cb.addTextMove(brdmove) is False:
            reason = self.REASONS[self.cb.getReason()] \
                if 0 <= self.cb.getReason() < len(self.REASONS) else "?"
            if CBstate.kingincheck:
                CBstate.kingincheck = False
                reason = "Your king is in check. " + reason
            self._speak("Error. " + reason + " in move " + brdmove)
            out["error"] = reason + " in move " + brdmove
            return out

        self._status("You: " + brdmove)
        self._checkvarious(out)
        board_before = self._RD_update(brdmove, board_before)
        self.fmove = (self.fmove + " " + brdmove).strip()
        self.movelist.append(brdmove)
        self._persist()
        if self.on_player_move:      # show the player's move in the UI
            try:                     # immediately (was: after the robot's
                self.on_player_move(brdmove)   # whole turn)
            except Exception:
                pass
        if out["game_over"]:
            # the human's move ended the game - no engine reply owed
            out["accepted"] = True
            self._notify_board()
            return out

        self._put("position startpos moves " + self.fmove)
        self._put("go movetime " + str(settings.get("stockfish.movetime_ms",
                                                    CBstate.movetime)))
        text = self._sget()
        smove = text[9:13]
        if self.cb.addTextMove(smove) is False:
            out["error"] = "engine produced illegal move " + smove
            out["accepted"] = True          # player's move did land
            self._notify_board()
            return out

        self.fmove = (self.fmove + " " + smove).strip()
        self.movelist.append(smove)
        out.update(accepted=True, computer_move=smove)
        self._status("Robot: " + smove)
        self._speak("I move " + smove[0:2] + " to " + smove[2:4])
        CBstate.cbstate = self.cb.getLastMoveType()   # feeds castling/en-passant
        if self.on_computer_move:    # show text+board NOW, before the arm
            try:                     # physically plays the move (was after)
                self.on_computer_move(smove)
            except Exception:
                pass
        if self.use_robot:
            try:
                RD = self._RD()
                RD.movepiece(smove[0:2], smove[2:4], board_before)
            except Exception as e:
                out["robot_error"] = str(e)
                self._status("Robot problem: %s" % e)
        self._checkvarious(out)
        self._persist()
        self._notify_board()
        return out

    def engine_first_move(self):
        """Robot plays white: make the opening engine move now."""
        out = {"accepted": False, "error": None, "player_move": None,
               "computer_move": None, "check": False,
               "game_over": False, "result_text": None}
        board_before = self.cb.getBoard()
        if self.fmove:
            self._put("position startpos moves " + self.fmove)
        self._put("go movetime " + str(settings.get("stockfish.movetime_ms",
                                                    CBstate.movetime)))
        smove = self._sget()[9:13]
        if self.cb.addTextMove(smove) is False:
            out["error"] = "engine produced illegal move " + smove
            return out
        self.fmove = (self.fmove + " " + smove).strip()
        self.movelist.append(smove)
        out.update(accepted=True, computer_move=smove)
        self._status("Robot: " + smove)
        CBstate.cbstate = self.cb.getLastMoveType()
        if self.on_computer_move:
            try:
                self.on_computer_move(smove)
            except Exception:
                pass
        if self.use_robot:
            try:
                RD = self._RD()
                RD.movepiece(smove[0:2], smove[2:4], board_before)
            except Exception as e:
                out["robot_error"] = str(e)
                self._status("Robot problem: %s" % e)
        self._checkvarious(out)
        self._persist()
        self._notify_board()
        return out

    def _RD_update(self, brdmove, board_before):
        if not self.use_robot:
            return board_before
        try:
            RD = self._RD()
            return RD.updateboard(brdmove[0:2], brdmove[2:4], board_before)
        except Exception:
            return board_before

    def _checkvarious(self, out):
        lt = self.cb.getLastMoveType()
        if lt not in (-1, 0):
            out["last_move_type"] = lt
        if self.cb.isCheck():
            out["check"] = True
            self._speak("Check!")
            self._status("Check!")
        if self.cb.isGameOver():
            codes = self.cb.getGameResult()
            texts = ("No result", "Checkmate - you win!",
                     "Checkmate - I win!", "Stalemate. Game over.",
                     "Draw by 50 moves rule", "Draw by threefold repetition")
            rt = texts[codes] if 0 <= codes < len(texts) else "Game over."
            self.game_over = True
            out["game_over"] = True
            out["result_text"] = rt
            out["result_code"] = codes
            self._speak(rt)
            self._status(rt)

    # ── persistence ──────────────────────────────────────────────────────────

    def _persist(self):
        """Save progress after every move; a finished game clears the save."""
        if self.game_over:
            game_state.clear()
            return
        game_state.save(self.movelist, settings.get("player.color", "white"),
                        settings.get("stockfish.level", 5))

    def end_game(self):
        """User chose to abandon: wipe the saved game."""
        game_state.clear()
        self.fmove = ""
        self.movelist = []
        self.game_over = False

    def takeback(self):
        """Undo the last full turn (player's move + engine's reply) and resync
        the engine. Engine-only: the human physically resets the board. Returns
        the number of moves removed (0 if nothing to undo)."""
        if self.game_over or not self.movelist:
            return 0
        n = 0
        while n < 2 and self.movelist:
            if self.cb.undo() is False:
                break
            self.movelist.pop()
            n += 1
        if n == 0:
            return 0
        self.fmove = " ".join(self.movelist)
        self._put("position startpos moves " + self.fmove)
        self._put("isready")
        self._get("readyok")
        game_state.save(self.movelist, settings.get("player.color", "white"),
                        settings.get("stockfish.level", 5))
        return n

    def board(self):
        return self.cb.getBoard()
