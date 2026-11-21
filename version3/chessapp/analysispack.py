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
import threading

import chess
import chess.pgn

from chessapp import analysis as AN
from chessapp import audio
from chessapp import game_state
from chessapp import narrative as NR
from chessapp import settings
from chessapp import voice as VO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "analysis")
# the selectable voices (2026-11-14): 'piper' was renamed to the MODEL it
# speaks with (alba) and GLaDOS joined it - both are Piper voices
VOICES = ("none", "espeak", "alba", "glados")

# The voice used until the player picks one (2026-11-18): espeak is the
# default - its cooked phrases ship with the repo (tiny) so a fresh install
# speaks out of the box, as the original espeak build did.  The player picks
# their own voice from the others (alba / GLaDOS need their own models).
# "None (fastest)" is an explicit choice, never the default.
DEFAULT_VOICE = "espeak"
PIPER_VOICES = ("alba", "glados")
VOICE_LABELS = {"none": "None (fastest)", "espeak": "espeak (fast)",
                "alba": "alba (slow)", "glados": "GLaDOS (slower)"}


def norm_voice(v):
    """The voice key for a stored setting ('piper' is the old name of
    alba) - 'none' when it is not one of the selectable voices."""
    if v == "piper":
        return "alba"
    return v if v in VOICES else "none"

# The stored analysis + commentary format. Bump when the engine fields or
# the narrative output change shape/quality - a cached text.json with an
# older format is regenerated. v5 (2026-10-23): one recommendation source,
# net-gain claims, opponent threats, alternatives, no 'It' for players.
CACHE_FORMAT = 5


_CODE_DIGEST = None


def _code_digest():
    """Digest of the sources that PRODUCE the text. A cached text.json is
    stale the moment any of them changes - and forgetting to bump
    CACHE_FORMAT kept serving old text after code changes (2026-10-24).
    This makes staleness impossible: change the code, regenerate.
    Memoized (2026-11-06): the sources cannot change at runtime, and the
    analysis home now probes for late-arriving text every poll."""
    global _CODE_DIGEST
    if _CODE_DIGEST is not None:
        return _CODE_DIGEST
    import hashlib
    h = hashlib.md5()
    for name in ("analysis.py", "narrative.py", "chessfacts.py",
                 "chesspos.py", "openings.py"):
        try:
            with open(os.path.join(os.path.dirname(__file__), name),
                      "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    _CODE_DIGEST = h.hexdigest()[:12]
    return _CODE_DIGEST


def game_hash(game):
    """The ANALYSIS identity of a game: its moves and the player's side
    (2026-11-21).  The old hash covered the WHOLE saved dict - including
    the bookkeeping that legitimately changes (result filled at game end,
    stars, graveyard, level, 'started') - and the end-game flow even wrote
    last_game.json in two shapes (with and without 'graveyard'), so the
    SAME game kept acquiring new identities: generation runs scattered
    across cache dirs - each one _absorb-ing and deleting the last - while
    the analysis screen tracked a dead identity and sat at 0/42 for hours
    with the engine working under another name."""
    key = {"moves": list(game.get("moves") or []),
           "player_color": game.get("player_color")}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()
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
        # pick up cached text right away: another instance's background run
        # (pre-warm) may have produced it - the analysis home's Begin button
        # waits on this list (2026-10-23)
        if self.has_text():
            self._load_text()

    @property
    def live_moves(self):
        """The per-move analysis entries as they land.  SHARED across pack
        instances of the same game (2026-11-06): the idle prewarm runs the
        generation on ITS instance while the analysis screen holds another,
        and the position-strength graph read the screen's empty list - no
        graph at all.  One store per game fixes every reader."""
        return _LIVE.setdefault(self.hash, [])

    @live_moves.setter
    def live_moves(self, value):
        _LIVE[self.hash] = value

    def cancel(self):
        """Stop the current generation run. Files already written are kept -
        switching voices never throws away finished work."""
        self._run_id += 1
        cancel_runs(self.hash)

    def text_path(self):
        return os.path.join(self.dir, "text.json")

    def partial_path(self):
        return os.path.join(self.dir, "text.partial.json")

    def _absorb(self, depth, mine):
        """Steal the engine work of a SHORTER version of this game, and
        throw out caches that can never serve it again (2026-11-08).
        Entries are per POSITION - valid however much longer the game
        grows - so the prewarm of a 12-move game carries straight into the
        60-move final analysis; only the new moves get searched.  Returns
        the entries to resume from."""
        ucis = self.game.get("moves") or []
        keep, stale = None, []
        try:
            names = os.listdir(CACHE)
        except OSError:
            names = []
        for name in names:
            d = os.path.join(CACHE, name)
            if name == self.hash or not os.path.isdir(d):
                continue
            src = _read_cache(d)
            if src is not None and src[0] == depth:
                # a source may be LONGER than this game (a takeback undid
                # moves) - trim it to the common prefix; entries are per
                # position so the trimmed ones are exactly what is needed
                n = min(len(src[1]), len(ucis))
                if n and src[1][:n] == ucis[:n]:
                    src = (src[0], src[1][:n], src[2][:n], src[3])
                else:
                    src = None
            if src is not None and src[2]:
                if keep is None or len(src[2]) > len(keep[1][2]):
                    if keep is not None:
                        stale.append(keep[0])
                    keep = (d, src)
                else:
                    stale.append(d)
            else:
                # an earlier game, a dead depth, or unreadable leftovers
                stale.append(d)
        out = mine
        if keep is not None and len(keep[1][2]) > len(mine):
            out = list(keep[1][2])
            openings = keep[1][3]
            self._save_partial(depth, out, ucis[:len(out)], openings)
        for d in stale + ([keep[0]] if keep is not None else []):
            shutil.rmtree(d, ignore_errors=True)
        return out

    def _load_partial(self, depth):
        """Entries from an interrupted earlier run of the SAME digest, depth
        and game (2026-11-07): the resume source, so an app restart does not
        re-do the whole search - the Pi overheated re-analysing from scratch
        every time the UI was restarted mid-run."""
        try:
            with open(self.partial_path()) as f:
                data = json.load(f)
            if (data.get("format") != CACHE_FORMAT
                    or data.get("code") != _code_digest()
                    or int(data.get("depth", -1)) != int(depth)):
                return []
            ucis = data.get("ucis") or []
            entries = data.get("entries") or []
            game_ucis = self.game.get("moves") or []
            if ucis != game_ucis[:len(ucis)]:
                return []          # a different game - do not trust it
            return entries
        except (OSError, ValueError):
            return []

    def _save_partial(self, depth, entries, ucis, openings):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.partial_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"format": CACHE_FORMAT,
                           "code": _code_digest(),
                           "depth": int(depth),
                           "ucis": list(ucis),
                           "entries": list(entries),
                           "openings": list(openings)}, f)
            os.replace(tmp, self.partial_path())
        except OSError:
            pass

    def wav_path(self, ply, voice):
        return os.path.join(self.dir, "m%03d_%s.wav" % (ply, voice))

    def has_text(self):
        if not os.path.exists(self.text_path()):
            return False
        try:
            with open(self.text_path()) as f:
                data = json.load(f)
            return (data.get("format") == CACHE_FORMAT
                    and data.get("code") == _code_digest())
        except (OSError, ValueError):
            return False

    def move_count(self):
        return len(self.game.get("moves", []))

    def review_moves(self):
        """The per-move packages when the analysis exists; otherwise the raw
        game moves as minimal entries (number + notation + stars), so the
        review runs before - or without - the engine (2026-10-24)."""
        if self.moves:
            return self.moves
        out = []
        try:
            b = chess.Board()
            for i, u in enumerate(self.game.get("moves") or []):
                try:
                    mv = chess.Move.from_uci(u)
                    san = b.san(mv)
                    b.push(mv)
                except (ValueError, AssertionError):
                    break
                col = "white" if i % 2 == 0 else "black"
                out.append({
                    "ply": i + 1, "no": i // 2 + 1, "color": col,
                    "san": san, "uci": u,
                    "segments": [("icon", NR.glyph_of(san)),
                                 ("move", "%d%s%s" % (i // 2 + 1,
                                                      "." if col == "white"
                                                      else "...", san))],
                    "verdict": "", "next": "", "wdl": None,
                    "quality": "", "best": None, "next_uci": [],
                    "speech": "", "words": 0})
        except Exception:
            return out
        return out

    def board_after(self, upto):
        """ChessBoard layout after `upto` plies (for the board widget)."""
        from ChessBoard import ChessBoard
        b = ChessBoard()
        for u in self.game.get("moves", [])[:upto]:
            b.addTextMove(u)
        return b

    def generate(self, voice="none", on_progress=None, depth=None,
                 text_only=False):
        """Staged background generation. on_progress(done, total, stage).
        Cancellable between moves; one bad move never kills the run."""
        if depth is None:
            depth = int(settings.get("analysis.depth", 18))
        token = self._run_id          # cancel() invalidates this run
        os.makedirs(self.dir, exist_ok=True)
        self.live_moves = []
        if not self.has_text():
            with _text_lock(self.hash):
                if not self.has_text():     # another voice's run may have
                    # the old sounds describe the OLD text - drop them now,
                    # not when the text lands: the 'n/33 voice files loaded'
                    # count must stay honest while the re-analysis runs
                    if os.path.isdir(self.dir):
                        for fn in os.listdir(self.dir):
                            if fn.startswith("m") and fn.endswith(".wav"):
                                try:
                                    os.remove(os.path.join(self.dir, fn))
                                except OSError:
                                    pass
                    g = game_from_state(self.game)
                    cached = self._absorb(depth, self._load_partial(depth))
                    self.analysis = AN.analyze_game(
                        g, depth=depth,
                        stop=lambda: self._run_id != token,
                        progress=((lambda i, n: on_progress(i, n, "start", ""))
                                  if on_progress else None),
                        on_entry=self.live_moves.append,
                        cached=cached,
                        checkpoint=lambda e, u, o: self._save_partial(
                            depth, e, u, o))
                    self.moves = NR.per_move(self.analysis)
                    with open(self.text_path(), "w") as f:
                        json.dump({"format": CACHE_FORMAT,
                                   "code": _code_digest(),
                                   "depth": int(depth),
                                   "moves": self.moves,
                                   "analysis": AN.analysis_to_jsonable(
                                       self.analysis)},
                                  f, indent=1)
                    try:
                        os.remove(self.partial_path())
                    except OSError:
                        pass
        if not self.moves:
            self._load_text()
        if on_progress:
            n = max(1, len(self.moves))
            on_progress(n, n, "text", "")   # the text stage ends at n/n
        if text_only:
            return self.moves   # the in-game prewarm: narration (and with
                                # it the voices) may still change as the
                                # game grows - skip them until it is over
        # THE VOICES GENERATE IN THE BACKGROUND WHATEVER THE USER PICKED -
        # espeak first (it is fast), then piper - so switching to voice
        # analysis later finds the work already done (2026-10-24)
        for v in ("espeak",) + PIPER_VOICES:
            total = max(1, len(self.moves))
            pending = []
            done = 0
            for m in self.moves:
                if self._run_id != token:
                    return self.moves
                out = self.wav_path(m["ply"], v)
                speech = (m.get("speech") or "").strip()
                if os.path.exists(out) or not speech:
                    done += 1
                    if on_progress:
                        on_progress(done, total, "audio", v)
                else:
                    pending.append((m["ply"], speech, out))
            if v in PIPER_VOICES:
                # ONE model load for the whole batch (2026-11-08): loading
                # piper per sentence took minutes per move on the Pi 3B and
                # the background run never finished a game
                def _one(_n):
                    nonlocal done
                    done += 1
                    if on_progress:
                        on_progress(done, total, "audio", v)
                try:
                    VO.synth_piper_many([(s, o) for _, s, o in pending],
                                        model=VO.PIPER_MODELS[v],
                                        on_item=_one,
                                        should_stop=lambda: self._run_id != token)
                except Exception as e:
                    print("voice: %s batch failed: %s" % (v, e))
            else:
                for ply, speech, out in pending:
                    if self._run_id != token:
                        return self.moves
                    try:
                        VO.synth_espeak(speech, out)
                    except Exception as e:
                        # one bad line must not stop the rest of the game
                        print("voice: move %d failed: %s" % (ply, e))
                    done += 1
                    if on_progress:
                        on_progress(done, total, "audio", v)
        return self.moves

    def _load_text(self):
        try:
            with open(self.text_path()) as f:
                data = json.load(f)
            self.moves = data.get("moves", [])
            self.analysis = data.get("analysis")
            if self.analysis:
                self.live_moves = list(self.analysis.get("moves") or [])
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


# ── background generation (pre-warming) ──────────────────────────────────────
# One run per (game, voice): started explicitly by the Analysis home OR
# opportunistically during idle time (menus, the player's turn), so the audio
# is ready by the time the player opens the analysis. A run's progress is
# shared state, so any screen can show it - and nobody starts a second run
# over the top of a live one (two writers on the same wav would corrupt it).

_LOCK = threading.Lock()
_RUNS = {}
# The live per-move entries, keyed by game hash: shared by every pack
# instance of that game (the chart and the turning-point stars must see the
# entries whichever instance the generation runs on).
_LIVE = {}
_TEXT_LOCKS = {}
# The written/spoken TEXT is identical for every voice ('there will just be
# nothing to play it in voice-only mode') - so its generation is ONE shared
# stage per game. Switching voices used to restart the count at 0/33 while
# the engine was already at 3/33 (2026-10-24).
_TEXT_STATE = {}


def _text_lock(h):
    """One text generation at a TIME (2026-11-08: was one per game).  A
    growing game spawns a run per move-state and the newer run ABSORBS the
    older run's cache - the two must never write the cache together, so
    every run waits its turn and the newer one picks up the older one's
    finished work."""
    return _TEXT_LOCKS.setdefault("all", threading.Lock())


def generate_async(pack, voice, depth=None, text_only=False):
    """Start (or join) the staged generation for this game+voice. Returns the
    shared state dict: {done, total, state, error, alive}."""
    voice = voice or "none"     # one key per voice (prewarm passes None;
                                # without this it and the analysis home ran
                                # DUPLICATE full analyses, 2026-11-07)
    if depth is None:
        depth = int(settings.get("analysis.depth", 18))
    key = (pack.hash, voice)
    with _LOCK:
        st = _RUNS.get(key)
        if st is not None and st.get("alive"):
            return st
        st = {"done": 0, "total": max(1, pack.move_count()),
              "state": "start", "voice": "", "error": None, "alive": True}
        _RUNS[key] = st

    def run():
        def prog(d, n, s, v=""):
            st.update(done=d, total=n, state=s, voice=v)
            if s in ("start", "text"):
                ts = _TEXT_STATE.setdefault(
                    pack.hash, {"done": 0,
                                "total": max(1, pack.move_count()),
                                "state": s, "error": None, "alive": True})
                ts.update(done=d, total=n, state=s)
                if s == "text":
                    ts["alive"] = False   # the text stage is complete
        try:
            pack.generate(voice, depth=depth, on_progress=prog,
                      text_only=text_only)
        except Exception as e:
            st["error"] = str(e)
            ts = _TEXT_STATE.get(pack.hash)
            if ts is not None and ts.get("alive"):
                ts["error"] = str(e)
                ts["alive"] = False
        finally:
            st["alive"] = False

    threading.Thread(target=run, daemon=True).start()
    return st


# Measured on the Pi 3B (3 threads, 64 MB hash, 2026-11-08): the text stage
# costs per position a VERDICT search at full depth plus an ALTERNATIVES
# search at depth-3 with three lines.  Fitted to the benchmark:
#   verdict(d)      = 0.1849 * 1.2789^d seconds
#   alternatives(d) = 0.2112 * 1.389^d  seconds
_VERDICT_A, _VERDICT_B = 0.1849, 1.2789
_ALTS_A, _ALTS_B = 0.2112, 1.389


def estimate_text_seconds(depth, positions):
    """What the text stage costs at `depth` for `positions` analysed moves:
    both searches per position plus the start position's verdict pass."""
    d = int(depth)
    n = max(0, int(positions))
    verdict = _VERDICT_A * _VERDICT_B ** d
    alts = _ALTS_A * _ALTS_B ** max(10, d - 3)
    return verdict * (n + 1) + alts * n


def fmt_estimate(seconds):
    """"~19 min" / "~2.6 h" / "~32 h"."""
    mins = seconds / 60.0
    if mins < 90:
        return "~%d min" % round(mins)
    hours = mins / 60.0
    return ("~%.1f h" % hours) if hours < 20 else ("~%d h" % round(hours))


def cancel_runs(h):
    """Mark every run for this game dead the instant it is cancelled
    (2026-11-08): the UI must start a FRESH run instead of joining a
    doomed one, and the engine's in-flight search aborts via its stop
    hook rather than finishing a depth nobody wants."""
    with _LOCK:
        for (hh, _v), st in _RUNS.items():
            if hh == h:
                st["alive"] = False
                st["cancelled"] = True


def _read_cache(d):
    """(depth, ucis, entries, openings) from a cache dir's checkpoint or its
    finished text - None when the dir holds nothing usable."""
    try:
        with open(os.path.join(d, "text.partial.json")) as f:
            data = json.load(f)
        if (data.get("format") == CACHE_FORMAT
                and data.get("code") == _code_digest()):
            return (int(data.get("depth", -1)), data.get("ucis") or [],
                    data.get("entries") or [], data.get("openings") or [])
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(d, "text.json")) as f:
            data = json.load(f)
        if (data.get("format") == CACHE_FORMAT
                and data.get("code") == _code_digest()):
            an = data.get("analysis") or {}
            entries = an.get("moves") or []
            return (int(data.get("depth", -1)),
                    [e.get("uci") for e in entries], entries,
                    an.get("openings") or [])
    except (OSError, ValueError):
        pass
    return None


def load_live_pack():
    """The game still being played (data/game.json), if any.  Prewarming it
    during the player's turn and on menus means the finished game arrives
    already analysed (2026-11-08)."""
    try:
        data = game_state.load()
    except Exception:
        return None
    if not data or not data.get("moves"):
        return None
    return AnalysisPack(data)


def generation_state(pack, voice):
    # the text stage is shared across voices: the same words are being
    # written whatever the selected voice (and the same words get spoken -
    # only the sound differs), so every voice shows the same count
    voice = voice or "none"
    ts = _TEXT_STATE.get(pack.hash)
    if ts is not None and ts.get("alive"):
        return ts
    st = _RUNS.get((pack.hash, voice))
    if st is not None and st.get("alive"):
        return st
    # the idle prewarm runs under the shared "none" key - the analysis home
    # must SEE it, or the background generation looks dead while it works
    # (2026-11-08)
    for (h, _v), s in _RUNS.items():
        if h == pack.hash and s.get("alive"):
            return s
    return st


def top_turning(moves, n=3, player=None, exclude_last=True):
    """The game's most consequential moves: the biggest swings in the
    position score after a move - the PLAYER's moves only, and never the
    FINAL move (a checkmate is always the biggest swing, 2026-10-24)."""
    cand = list(moves)
    if exclude_last and len(cand) > 1:
        cand = cand[:-1]
    if player:
        cand = [m for m in cand if m.get("color") == player]
    ranked = sorted(
        cand,
        key=lambda m: -abs((m.get("eval_cp") or 0)
                           - (m.get("eval_before_cp") or 0)))
    return [m["ply"] for m in ranked[:n] if m.get("ply")]


def prewarm(voice=None):
    """Idle-time generation.  The game being played FIRST (2026-11-08):
    analysing it move by move during the player's turn and on menus means
    the finished game arrives already analysed - only its last moves and
    the voices are left.  Then, if a last finished game has ungenerated
    work, quietly do it in the background. The voices are generated whatever the
    user picked (espeak then piper), so this is NOT gated on the selection -
    a reboot used to find nothing started at all (2026-10-24)."""
    try:
        live = load_live_pack()
        if live is not None:
            return generate_async(live, voice, text_only=True)
        pack = load_pack()
        if pack is None:
            return None
        # stale analysis/commentary (old cache format) is work too
        if not pack.has_text():
            return generate_async(pack, voice)
        # already complete? BOTH voices are generated in the background now
        n = pack.move_count()
        if n and all(pack.ready(i + 1, v) for i in range(n)
                     for v in ("espeak",) + PIPER_VOICES):
            return None
        return generate_async(pack, voice)
    except Exception:
        return None


def load_pack():
    """The last finished game's pack.  Caches of earlier games are discarded
    by AnalysisPack._absorb, which runs at generation time - deleting them
    here would throw away the prefix cache the final run absorbs (2026-11-08)."""
    data = game_state.load_last()
    if not data:
        return None
    return AnalysisPack(data)
