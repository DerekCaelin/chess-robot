# chessapp/narrative.py - turn an analysis dict into a game narrative, in two
# renderings:
#   screen - move numbers + notation:  "10.Nxb5!   A strong choice. ..."
#   speech - spoken moves, no numbers: "Knight captures on b five. A strong
#            choice. ..."
#
# Phrasings come from per-category bags WITHOUT repetition until a bag empties,
# so a narrative never repeats itself within a game. Length scales with impact:
# quiet moves are usually silent (a one-line bridge covers a whole quiet
# stretch), inaccuracies get 1-2 sentences with the constructive alternative,
# and big moments get 2-3.
#
#   python3 -m chessapp.narrative games/game_1233404.pgn [--seed N] [--style both]
import json
import os
import random

from chessapp import analysis as AN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Harvested human phrasing (tools/harvest_phrases.py): commentary sentences
# mined from annotated games, keyed by (concept, polarity, colour). The
# narrator prefers them over its own banks - variety with a human voice.
# Loaded once; missing/empty is fine (the built-in banks remain).
_PHRASE_LIB = None


def phrase_lib():
    global _PHRASE_LIB
    if _PHRASE_LIB is None:
        try:
            with open(os.path.join(os.path.dirname(__file__),
                                   "phraselib.json")) as f:
                _PHRASE_LIB = json.load(f)
        except (OSError, ValueError):
            _PHRASE_LIB = {}
    return _PHRASE_LIB

PIECE_WORDS = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen",
               "K": "king"}


# ── word-level helpers ────────────────────────────────────────────────────────

def spoken_san(san):
    """Human words for a SAN move: 'Bxb5+' -> 'bishop captures on b5 with
    check', 'Nbd7' -> 'knight from b to d7', 'O-O-O' -> 'castles queenside'."""
    raw = san
    check = "+" in raw
    mate = "#" in raw
    s = raw.replace("+", "").replace("#", "").replace("!", "").replace("?", "")
    if s in ("O-O", "O-O-O"):
        words = "castles kingside" if s == "O-O" else "castles queenside"
    else:
        promo = ""
        if "=" in s:
            s, pr = s.split("=", 1)
            promo = " promotes to %s" % PIECE_WORDS.get(pr[0], "queen")
        piece = "pawn"
        if s and s[0] in PIECE_WORDS:
            piece = PIECE_WORDS[s[0]]
            s = s[1:]
        capture = "x" in s
        if capture:
            s = s.replace("x", "")
        dest = s[-2:]
        origin = s[:-2]
        words = piece
        if origin:
            words += " from " + origin
        words += (" captures on " if capture else " to ") + dest + promo
    if check:
        words += " with check"
    if mate:
        words += ", checkmate"
    return words


def mark_for(quality):
    return {"brilliant": "!!", "great": "!", "best": "",
            "inaccuracy": "?!", "mistake": "?", "blunder": "??"}.get(quality, "")


# ── phrase banks ─────────────────────────────────────────────────────────────
# IMPACT entries are past-participle phrases: every template uses them after
# 'which would have' / 'that would have' - never after 'keeping'/'kept'.

IMPACT_GOOD = [
    "kept the initiative",
    "kept the pressure on",
    "kept the attack going",
    "won material on the spot",
    "finished the game immediately",
    "converted the advantage",
    "kept the extra material",
    "maintained the bind",
    "avoided the counterplay",
    "led to a forced win",
    "kept {opponent} tied down",
    "left {opponent} with nothing to do",
    "kept {Color} on top",
]

IMPACT_NEUTRAL = [
    "kept the balance",
    "kept things under control",
    "stayed in the game",
    "consolidated the position",
    "kept the position healthy",
    "held the initiative",
]

IMPACT_HOLD = [
    "held the balance",
    "kept the game going",
    "made {opponent} work for it",
    "kept {Color} in the game",
    "kept the king safe",
    "asked the real questions",
    "stretched the defence",
]

IMPACT_LOST = [
    "made {opponent} work for it",
    "kept the game going",
    "held out longer",
    "stretched the game",
    "kept {Color} fighting",
    "asked one more question",
]

OPENINGS = [
    (["e2e4", "e7e5"], "an open game"),
    (["e2e4", "c7c5"], "a Sicilian"),
    (["e2e4", "e7e6"], "a French Defence"),
    (["e2e4", "c7c6"], "a Caro-Kann"),
    (["d2d4", "d7d5"], "a closed game"),
    (["d2d4"], "a queen's pawn game"),
    (["e2e4"], "a king's pawn game"),
    (["c2c4"], "an English opening"),
]

INTRO = [
    ("{White} against {Black}, {event_year}.",),
    ("This is {White} versus {Black}, {event_year}.",),
    ("At {event_year}, {White} has White and {Black} is Black.",),
    ("A game between {White} and {Black}, {event_year}.",),
    ("On the board: {White} versus {Black}.",),
    ("This is {White} against {Black}.", "It opens as {opening}."),
    ("{White} versus {Black}, {event_year}.", "{opening_cap}, so "
     "anything can happen."),
    ("Here is {White} against {Black}.", "It opens as {opening}."),
]

QUIET = [
    ("The next {n} moves are quiet manoeuvring.",),
    ("Both sides spend {n} moves improving their position.",),
    ("{n} moves of careful development follow.",),
    ("Nothing critical happens for {n} moves.",),
    ("A stretch of shuffling follows: {n} moves without a capture.",),
    ("The players take {n} moves to rearrange their pieces.",),
    ("For {n} moves, it is a waiting game.",),
]

PRAISE_QUIET = [
    ("A solid move.",),
    ("A sensible move.",),
    ("A natural developing move.",),
    ("Steady play.",),
]

PRAISE = [
    ("The best move.",),
    ("The best move in the position.",),
    ("Exactly the best move.",),
    ("The right move.",),
]

GREAT = [
    ("An excellent move!",),
    ("A strong choice.",),
    ("The clearest path in the position.",),
    ("Very strong.",),
    ("A sharp move!",),
    ("The best move by some distance.",),
    ("Well played!",),
]

BRILLIANT = [
    ("A brilliant move!",),
    ("A brilliant move!", "{Color} has seen further than the opponent."),
    ("A lovely move!",),
    ("Beautifully played!",),
    ("A brilliant finish!",),
]

INACCURACY = [
    ("A small imprecision.",),
    ("Playable.", "{Color} could have played {best}, which would have "
     "{impact}."),
    ("A slight loosening.", "{Best} was the accurate move. That would "
     "have {impact}."),
    ("This gives a little ground.", "{Best} was better: that would have "
     "{impact}."),
    ("Not the cleanest choice.", "{Color} could have gone with {best}, which "
     "would have {impact}."),
    ("The position tolerates it.", "{best} was stronger. That would have "
     "{impact}."),
    ("A quieter alternative was better.", "{Best} would have {impact}."),
]

MISTAKE = [
    ("A mistake under pressure.", "{Color} could have played {best}, which "
     "would have {impact}."),
    ("This lets {opponent} in.", "The right move was {best}. That would "
     "have {impact}."),
    ("The initiative changes hands here.", "{Color} should have chosen "
     "{best}, which would have {impact}."),
    ("This gives up the advantage.", "Better was {best}; that would have "
     "{impact}."),
    ("An inaccuracy that costs real ground.", "With {best}, that would have "
     "{impact}."),
    ("This is not the accurate move.", "{Best} was the way to go. That "
     "would have {impact}."),
    ("A step in the wrong direction.", "{Color} had {best}, which would have "
     "{impact}."),
]

BLUNDER = [
    ("A serious error.", "{Color} could have played {best}, which would have "
     "{impact}.", "Instead, the game swings the other way."),
    ("This throws the advantage away.", "{Best} would have {impact}."),
    ("The game turns here.", "With {best}, that would have {impact}."),
    ("This loses material.", "{Color} had {best}, which would have "
     "{impact}."),
    ("The decisive mistake.", "{Best} was the move. It would have "
     "{impact}.", "After this, {opponent} is in control."),
    ("A blunder in a difficult position.", "{Best} would have {impact}."),
    ("A costly mistake.", "{Color} had {best}, which would have {impact}."),
]

BLUNDER_SWING = [
    ("A serious error.", "{Color} could have played {best}, which would have "
     "{impact}.", "The position goes from {eval_before} to {eval_after}."),
    ("A costly mistake.", "The game was {eval_before}; it is now "
     "{eval_after}.", "With {best}, that would have {impact}."),
    ("This is where it slips away.", "Before the move: {eval_before}. After: "
     "{eval_after}.", "{best} would have {impact}."),
]

# the same stories told in winning chances (engine WDL) when available
BLUNDER_SWING_WDL = [
    ("A serious error.", "{Color} could have played {best}, which would have "
     "{impact}.", "{Color}'s winning chances fall from {wp_before}% to "
     "{wp_after}%."),
    ("A costly mistake.", "{Color}'s winning chances fall from {wp_before}% "
     "to {wp_after}%.", "With {best}, that would have {impact}."),
    ("This is where it slips away.", "The winning chances fall from "
     "{wp_before}% to {wp_after}%.", "{best} would have {impact}."),
]

TURNING_GOOD = [
    ("A turning point.",),
    ("The momentum changes hands.",),
    ("This is the decisive moment.",),
    ("From here, {Color} is in control.",),
    ("The game pivots on this move.",),
]

TURNING_BAD = [
    ("A turning point.",),
    ("This is the decisive moment.",),
    ("The momentum changes hands.",),
    ("From here, {opponent} is in control.",),
    ("The game pivots on this move.",),
]

DECISION = [
    ("Two reasonable plans here.",),
    ("A real choice in the position.", "{Best} was the alternative."),
    ("Here the game could have gone several ways.",),
    ("A critical decision.",),
]

FINISH = [
    ("",),
]

# The conclusion: the checkmate is the END of the story, and the story is told
# according to how the game actually went (arc = _game_arc below). {final} is
# the mating move (screen: notation, speech: spoken words).
OUTRO_MATE = {
    "dominant": [
        ("Checkmate.", "{winner} closes out a strong game."),
        ("Checkmate.", "{winner} was in control from early on and never let "
         "it slip."),
        ("A strong game by {winner}. The finish was never in doubt.",),
        ("Checkmate.", "The advantage was never really in question."),
        ("{winner} finishes a commanding game with {final}, checkmate.",),
        ("Checkmate.", "{winner} closes out a strong game.", "The decisive "
         "moment came on move {turn_no}."),
    ],
    "close": [
        ("Checkmate.", "A tight game - {winner} edges it at the end."),
        ("After a game that went down to the wire, {winner} takes the win "
         "with {final}, checkmate."),
        ("Checkmate.", "Both sides had their moments; {winner} finds the "
         "finish."),
        ("A close game throughout.", "{winner} finally breaks through."),
    ],
    "swingy": [
        ("After a game that swung back and forth, {winner} secures the win "
         "with {final}, checkmate."),
        ("Checkmate.", "The game changed hands more than once before "
         "{winner} finished it."),
        ("After a game that swung back and forth, {winner} comes out on top.",),
        ("Checkmate.", "{winner} wins a game that was never settled until "
         "the last move."),
    ],
    "comeback": [
        ("Checkmate.", "A remarkable turnaround - {winner} was in trouble "
         "earlier but finds the finish."),
        ("A real comeback.", "{winner} was worse for a long stretch, and "
         "ends the game with {final}, checkmate."),
        ("Checkmate.", "From a difficult position to a win - {winner} turns "
         "the game around."),
        ("Checkmate.", "{winner} never gave up the fight and is rewarded "
         "for it."),
    ],
}

OUTRO_OTHER = {
    "dominant": [
        ("{result_line}", "{winner} was in control throughout."),
        ("{result_line}", "A strong game by {winner}."),
        ("{result_line}", "{winner} never let the advantage slip."),
    ],
    "close": [
        ("{result_line}", "A tight game - it could have gone either way."),
        ("{result_line}", "Both sides had their moments."),
    ],
    "swingy": [
        ("{result_line}", "A game that swung back and forth."),
        ("{result_line}", "The lead changed hands before {winner} settled "
         "it."),
    ],
    "comeback": [
        ("{result_line}", "A remarkable turnaround by {winner}."),
        ("{result_line}", "{winner} was in trouble earlier and turns it "
         "around."),
    ],
}

# What a move DOES to the position (chessfacts): each kind has (screen, speech)
# phrasings - the text track stays succinct, the spoken track fuller.
FACT_PHRASES = {
    "mate": [("Checkmate.", "Checkmate.")],
    "double_check": [("Double check!", "A double check - two pieces give "
                      "check at once!")],
    "check": [("Gives check.", "Check with the {piece}."),
              ("Check.", "The {piece} gives check.")],
    "exposes": [
        ("The {piece} on {square} hangs to the {attacker}.",
         "This leaves the {piece} on {square} undefended - the {attacker} "
         "can simply take it."),
        ("Loose {piece} on {square}.",
         "After this, the {piece} on {square} is exposed to the {attacker}."),
        ("The {piece} on {square} is left loose.",
         "The {piece} on {square} is now standing without cover, and the "
         "{attacker} is ready to pounce."),
    ],
    "threatens": [
        ("Threatens the {piece}.",
         "The {piece} on {square} is now under attack."),
        ("Attacks the {piece}.",
         "This puts pressure on the {piece} on {square}."),
    ],
    "fork": [
        ("Forks the {first} and the {second}.",
         "A fork: the {piece} attacks the {first} and the {second} at the "
         "same time."),
    ],
    "pin": [
        ("Pins the {piece} to the {target}.",
         "The {piece} on {square} is pinned to the {target} and cannot "
         "move."),
        ("A pin on the {piece}.",
         "The {piece} on {square} finds itself pinned against the "
         "{target}."),
        ("Pins the {piece} down.",
         "The {piece} on {square} is tied down; moving it would expose "
         "the {target}."),
    ],
    "opens_line": [
        ("Opens the {line} for the {piece}.",
         "The {line} opens up, giving the {piece} a clear line of attack."),
    ],
    "promotion": [("Promotes to {piece}.",
                   "The pawn promotes to a {piece}.")],
    "passed": [("A passed pawn.",
                "The pawn on {square} is now passed - nothing stands in "
                "its way.")],
    "isolated": [("An isolated pawn.",
                  "The pawn on {square} is isolated - no friendly pawn can "
                  "defend it.")],
    "capture": [("Takes the {piece}.",
                 "This captures the {piece} on {square}."),
                ("Wins the {piece}.",
                 "{Color} picks up the {piece} on {square}.")],
    "castles": [("Castles {side}.",
                 "Castling {side} brings the king to safety.")],
    "sacrifice": [
        ("The {piece} is offered up.",
         "A sacrifice - {Color} gives up the {piece} for the attack."),
        ("The {piece} hangs, by design.",
         "{Color} offers the {piece}, and the attack comes first."),
        ("An offer of material.",
         "{Color} gives up the {piece} to keep the attack going."),
    ],
}

FACT_PRIORITY = ["mate", "double_check", "fork", "pin", "exposes", "threatens",
                 "opens_line", "promotion", "passed", "capture", "castles",
                 "check", "isolated"]

OPENING_LINES = [
    ("{Color} enters {opening} with {move}.",),
    ("This is {opening}.",),
    ("The game enters {opening}.",),
    ("{opening_cap} is on the board.",),
]

# short verdicts - used when the recommended-line analysis follows and carries
# the alternative itself
VERDICT_SHORT = {
    "best": [("The best move.",), ("Exactly the best move.",),
             ("The strongest move on the board.",)],
    "good": [("A good move.",), ("Solid.",), ("Sensible.",)],
    "great": [("An excellent move!",), ("Very strong.",), ("Well played!",)],
    "brilliant": [("A brilliant move!",), ("A lovely move!",)],
    "inaccuracy": [("A small imprecision.",),
                   ("Not the cleanest choice.",),
                   ("A slight loosening.",)],
    "mistake": [("A mistake under pressure.",),
                ("This lets {opponent} in.",),
                ("This gives up the advantage.",),
                ("A step in the wrong direction.",),
                ("An inaccuracy that costs real ground.",)],
    "blunder": [("A serious error.",),
                ("This loses material.",),
                ("A costly mistake.",),
                ("The game turns here.",),
                ("A blunder in a difficult position.",)],
}

# the recommended line: lead-in phrasings
LINE_LEAD = [
    ("A better alternative would be",),
    ("A better move would have been",),
    ("The move was",),
    ("{Color} could have played",),
    ("Better was",),
]
# a VARIATION leads differently: the series includes the opponent's replies,
# so "White could have played e4, e6, Nc3..." is not true of it (2026-10-24)
LINE_LEAD_LONG = [
    ("A better line would have been",),
    ("A better continuation was",),
    ("The line was",),
]

# phase transitions
PHASE_TO_MID = ["As the early game transitions to the mid game,",
                "With development complete,", "The opening is over,",
                "As the pieces come out,"]
PHASE_TO_END = ["As the game enters the endgame,",
                "With the material thinning out,", "The endgame arrives,"]
PHASE_POS = {
    "center": ["{color} controls the center",
               "{color} has a firmer grip on the center",
               "{color} dominates the central squares"],
    "king_safety": ["{color}'s pawn shield provides strong protection for "
                    "the king", "{color}'s king sits safely behind its pawns",
                    "{color} has the safer king"],
    "bishop_pair": ["{color} holds the bishop pair",
                    "{color} has the two bishops"],
    "rook_files": ["{color}'s rooks are active on open files",
                   "{color} has the more active rooks"],
    "space": ["{color} has more space to work with"],
    "development": ["{color} has completed development"],
    "outposts": ["{color}'s knights find good outposts"],
    "passed": ["{color} has a passed pawn to work with"],
}

# closing notes
HIGHLIGHT = ["{Winner} made good use of {pieces}.",
             "{Winner}'s {pieces} did the talking.",
             "The {pieces} carried {Winner}'s game."]
HIGHLIGHT_MOTIF = ["{Winner}'s {motif} made the difference.",
                   "The {motif} on move {n} set {Winner} on the way."]
MISTAKE_HANG = ["{Loser} might have had more success without losing the "
                "{piece} without gaining anything in return.",
                "Losing the {piece} for nothing cost {Loser} the game."]
MISTAKE_MOTIF = ["{Loser} could have obtained a better outcome if the "
                 "{motif} had been spotted in time.",
                 "The {motif} went unnoticed, and {Loser} paid for it."]
MISTAKE_GENERIC = ["With a little more care on move {n}, {Loser} could have "
                   "kept the game going.",
                   "{Loser} will look again at move {n}."]

TEMPLATES = {"intro": INTRO, "quiet": QUIET, "praise": PRAISE,
             "praise_quiet": PRAISE_QUIET, "great": GREAT,
             "brilliant": BRILLIANT, "inaccuracy": INACCURACY,
             "mistake": MISTAKE, "blunder": BLUNDER,
             "blunder_swing": BLUNDER_SWING,
             "blunder_swing_wdl": BLUNDER_SWING_WDL,
             "turning_good": TURNING_GOOD, "turning_bad": TURNING_BAD,
             "decision": DECISION, "finish": FINISH,
             "opening": OPENING_LINES, "line_lead": LINE_LEAD,
             "line_lead_long": LINE_LEAD_LONG}
for _k, _v in VERDICT_SHORT.items():
    TEMPLATES["verdict_" + _k] = _v
for _arc in OUTRO_MATE:
    TEMPLATES["outro_%s_mate" % _arc] = OUTRO_MATE[_arc]
    TEMPLATES["outro_%s" % _arc] = OUTRO_OTHER[_arc]

TIER_LIMIT = {1: 1, 2: 2, 3: 3}


class Teller:
    """Shuffle-bag template picker: every phrasing is used before any repeats."""

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.seed = seed
        self.bags = {}

    def pick(self, category, max_sents=3):
        pool = [t for t in TEMPLATES[category] if len(t) <= max_sents] or \
            TEMPLATES[category]
        bag = self.bags.setdefault(category, [])
        if not bag:
            bag.extend(pool)
            self.rng.shuffle(bag)
        return bag.pop()

    def fact(self, kind):
        """(screen, speech) phrasing pair for a positional fact."""
        pool = FACT_PHRASES.get(kind) or []
        if not pool:
            return None
        bag = self.bags.setdefault("fact:" + kind, [])
        if not bag:
            bag.extend(pool)
            self.rng.shuffle(bag)
        return bag.pop()

    def lib(self, key, pool):
        """Shuffle-bag pick from a harvested human-phrasing bucket."""
        if not pool:
            return None
        bag = self.bags.setdefault("lib:" + key, [])
        if not bag:
            bag.extend(pool)
            self.rng.shuffle(bag)
        return bag.pop()

    def phrase(self, bank):
        return self.rng.choice(bank)


# ── narrative assembly ───────────────────────────────────────────────────────

def wdl_phrase(wdl):
    """Human read of the engine's win/draw/loss probabilities (white POV)."""
    try:
        w, d, l = (int(x) for x in wdl)
    except (TypeError, ValueError):
        return ""
    tot = max(1, w + d + l)
    wp, bp = 100 * w // tot, 100 * l // tot
    if wp >= 70:
        return "White is winning"
    if bp >= 70:
        return "Black is winning"
    if wp >= 55:
        return "White is better"
    if bp >= 55:
        return "Black is better"
    return "the game is balanced"


def wdl_pct(wdl):
    try:
        w, d, l = (int(x) for x in wdl)
    except (TypeError, ValueError):
        return 0
    tot = max(1, w + d + l)
    return int(round(100.0 * w / tot))


def _wp(wdl, color):
    """Winning probability in % for the given color (engine WDL, white POV)."""
    try:
        w, d, l = (int(x) for x in wdl)
    except (TypeError, ValueError):
        return 0
    tot = max(1, w + d + l)
    return int(round(100.0 * (w if color == "white" else l) / tot))


# what a move achieves - and what it merely THREATENS. The gain often lands
# later in the engine's line ('7.Qb3 ... picks up a pawn with Bxf7+'): that
# is a threat of the move, not its result (2026-10-23).
ACHIEVE_MATE = [("That ends the game.", "That ends the game."),
                ("Mate follows.", "Mate follows."),
                ("The game is over by force.", "The game is over by force.")]
ACHIEVE_WIN = [("{Color} wins {what}.", "{Color} wins {what}."),
               ("{Color} picks up {what}.", "{Color} picks up {what}.")]
THREAT_WIN = [("{Color} threatens to win {what}{with}.",
               "{Color} threatens to win {what}{with_spk}."),
              ("The threat is to win {what}{with}.",
               "The threat is to win {what}{with_spk}.")]

# the aspect of the position that CHANGED most on the move (concept deltas
# from the analysis) - the informative core of an otherwise quiet comment
CONCEPT_NOTES = {
    "center": (["{Color}'s grip on the center grows.",
                "The center comes under {Color}'s control.",
                "{Color} gains a stronger say in the center."],
               ["{Color}'s grip on the center loosens.",
                "Some control of the center is given up."]),
    "king_safety": (["{Color}'s king looks safer.",
                     "The king tucks in behind the pawns for {Color}."],
                    ["{Color}'s king cover weakens.",
                     "The {Color} king looks more exposed."]),
    "development": (["More {Color} pieces join the game.",
                     "{Color} brings another piece into play."],
                    ["{Color} falls behind in development."]),
    "space": (["{Color} gains room to work.",
               "More space for the {Color} pieces."],
              ["{Color}'s space is squeezed."]),
    "structure": (["{Color}'s pawn structure improves.",
                   "The pawns work better for {Color} now."],
                  ["{Color}'s pawn structure is weakened."]),
    "outposts": (["A {Color} piece settles on a protected outpost."],
                 ["{Color} loses an outpost."]),
    "bishop_pair": (["{Color} keeps the bishop pair."],
                    ["{Color} loses the bishop pair."]),
    "rook_files": (["{Color}'s rooks find open files."],
                   ["{Color}'s rooks are cut off from the open files."]),
}
# a delta must clear this to say anything (the metrics move on quiet moves)
CONCEPT_MIN = {"center": 2, "king_safety": 2, "development": 2,
               "space": 6, "structure": 2, "outposts": 1,
               "bishop_pair": 1, "rook_files": 1}


PIECE_WORDS_S = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen",
                 "K": "king"}


def _piece_of_san(san):
    return PIECE_WORDS_S.get(san[:1], "pawn")


def _line_pair(teller, e, s):
    """The recommended-line analysis: lead-in, the move series with its named
    events, and what the line achieves. [a better alternative would be]
    [move series] [impact]."""
    bl = e.get("best_line") or {}
    plies = bl.get("plies") or []
    if len(plies) < 2:
        return None
    concl = bl.get("concl") or {}
    # the series is a VARIATION with both sides replying: lead it as a line
    # and number the moves, matching the prediction style
    lead = teller.pick("line_lead_long", 1)[0].format(**s)
    scr, spk, marks = [], [], []
    base_no = e["no"]
    for i, pl in enumerate(plies):
        num = base_no + i // 2
        dots = "." if pl.get("color") == "white" else "..."
        w_scr = "%d%s%s" % (num, dots, pl["san"])
        w_spk = spoken_san(pl["san"])
        if pl.get("forced"):
            w_scr += " (forced)"
            if i:
                w_spk = "the forced " + w_spk
        for k in ("fork", "pin", "double_check"):
            if k in pl.get("kinds", []):
                marks.append(k.replace("_", " "))
        scr.append(w_scr)
        spk.append(w_spk)
    scr_series = ", ".join(scr)
    spk_series = spk[0] + (", followed by " + ", ".join(spk[1:])
                           if len(spk) > 1 else "")
    if marks and concl.get("kind") != "mate":
        # a fork or pin on a mating line is noise - the mate says it all
        note = marks[0]
        scr_series += " (%s)" % note
        spk_series += ", with a %s" % note
    if concl.get("kind") == "mate":
        end_scr = end_spk = "and that is checkmate"
    elif concl.get("kind") == "wins":
        what = concl.get("what") or "material"
        mv = concl.get("move") or ""
        tail = (" with %s" % mv) if mv else ""
        tail_spk = (" with %s" % spoken_san(mv)) if mv else ""
        end_scr = "winning %s%s" % (what, tail)
        end_spk = "winning %s%s" % (what, tail_spk)
    else:
        end_scr = end_spk = ""
    def _assemble(lead_s, series_s, end_s):
        return ("%s %s%s." % (lead_s, series_s, (", " + end_s) if end_s
                              else "")).strip()
    return (_assemble(lead, scr_series, end_scr),
            _assemble(lead, spk_series, end_spk), {})


def _score_line(cp):
    """What the engine's SCORE says about the position (white POV cp) - the
    non-neutral half of the phase summary. '' when the score is quiet."""
    if cp >= 300:
        return "White has a clear advantage in positional strength"
    if cp >= 90:
        return "White has the better position"
    if cp <= -300:
        return "Black has a clear advantage in positional strength"
    if cp <= -90:
        return "Black has the better position"
    return ""


def _phase_note(teller, ph, s):
    """The phase summary is scoped and honest (2026-10-23): a neutral
    material statement 'in terms of material', then - when the engine's
    score says otherwise - the advantage statement, joined with 'but'.
    'White and Black are evenly matched' alone claimed of the position what
    is only true of material."""
    rep = ph.get("report") or {}
    d = rep.get("material", 0)
    lead = "White" if d > 0 else "Black"
    if abs(d) <= 2:
        core = "White and Black are evenly matched in terms of material"
        if abs(d) >= 1:
            core += ", with %s up a single pawn" % lead
    elif abs(d) <= 4:
        core = "in terms of material, %s is up the exchange" % lead
    elif abs(d) <= 8:
        core = "in terms of material, %s is up a piece" % lead
    else:
        core = "in terms of material, %s is up significant material" % lead
    head = teller.phrase(PHASE_TO_MID if ph["to"] == "middlegame"
                         else PHASE_TO_END)
    score = _score_line(ph.get("eval_cp", 0))
    out = "%s %s" % (head, core)
    if score:
        # 'but' when the score contradicts the material impression
        contrast = (abs(d) <= 2 and not score.startswith("White and")) or \
                   (d > 0 and score.startswith("Black")) or \
                   (d < 0 and score.startswith("White"))
        out += (", but " if contrast else ", and ") + score
    out += "."
    obs = _positional_notes(teller, rep, s)
    if obs:
        out += " " + obs[0]
    return (out, out, {})


def _positional_notes(teller, rep, s):
    """1-3 measurable positional observations, strongest first."""
    out = []
    w, b = rep.get("center", (0, 0))
    if abs(w - b) >= 3:
        out.append((("center", "White" if w > b else "Black")))
    ws, bs = rep.get("king_safety", (0, 0))
    if abs(ws - bs) >= 2:
        out.append(("king_safety", "White" if ws > bs else "Black"))
    if rep.get("bishop_pair", (False, False))[0]:
        out.append(("bishop_pair", "White"))
    elif rep.get("bishop_pair", (False, False))[1]:
        out.append(("bishop_pair", "Black"))
    rw, rb = rep.get("rook_files", (0, 0))
    if abs(rw - rb) >= 1 and max(rw, rb) >= 2:
        out.append(("rook_files", "White" if rw > rb else "Black"))
    sp = rep.get("space", (0, 0))
    if abs(sp[0] - sp[1]) >= 25:
        out.append(("space", "White" if sp[0] > sp[1] else "Black"))
    out2 = []
    for kind, color in out[:3]:
        phrase = teller.phrase(PHASE_POS.get(kind, ["{color} is better"]))
        out2.append(phrase.format(color=color) + ".")
    return out2


def _closing_notes(analysis, teller, s):
    """The conclusion states only checkable things: the move the game turned
    on - with the engine's better line - and the winner's strongest move with
    what it achieved (2026-10-23: the earlier free-text synthesis claimed
    things the board could not back up)."""
    moves = analysis["moves"]
    res = analysis["meta"].get("result")
    winner = ("white" if res == "1-0" else "black" if res == "0-1" else None)
    if not moves or winner is None:
        return []
    loser = "black" if winner == "white" else "white"
    s = dict(s, Winner=winner.capitalize(), Loser=loser.capitalize())
    notes = []

    def move_slots(m):
        no, dots = m["no"], ("." if m["color"] == "white" else "...")
        out = dict(s, Color=m["color"].capitalize(),
                   opponent=("black" if m["color"] == "white" else "white")
                   .capitalize(),
                   move_screen="%d%s%s" % (no, dots, m["san"]),
                   move_speech=spoken_san(m["san"]),
                   best_screen="%d%s%s" % (no, dots, m.get("best_san", "")),
                   best_speech=spoken_san(m.get("best_san") or ""),
                   swing=abs(m.get("eval_cp", 0) - m.get("eval_before_cp", 0)),
                   wp_before=_wp(m.get("wdl_before"), m["color"]),
                   wp_after=_wp(m.get("wdl"), m["color"]),
                   impact="won material")
        bi = m.get("best_impact") or {}
        if bi.get("kind") == "wins":
            out["impact"] = ("finished the game"
                             if bi.get("what") == "the game"
                             else "won %s" % bi.get("what", "material"))
        return out

    # the loser's costliest move, with the engine's line against it
    big = [m for m in moves if m["color"] == loser and m["loss_cp"] >= 150]
    big.sort(key=lambda m: -m["loss_cp"])
    if big:
        m = big[0]
        ms = move_slots(m)
        head = "The game turned on move %d." % m["no"]
        pair = (_line_pair(teller, m, ms)
                if m.get("best_line") and m["uci"] != m.get("best") else None)
        if pair:
            notes.append((head + " " + pair[0].format(**pair[2]),
                          head + " " + pair[1].format(**pair[2]), {}))
        elif m.get("mate_after") is not None and m["mate_after"] < 0:
            notes.append((head + " %s allows a forced mate."
                          % ms["Color"],
                          head + " %s allows a forced mate."
                          % ms["Color"], {}))
        else:
            notes.append((head, head, {}))

    # the winner's strongest move and what it achieved
    strong = [m for m in moves if m["color"] == winner
              and m["quality"] in ("great", "brilliant")]
    if strong:
        m = max(strong, key=lambda m: abs(m.get("eval_cp", 0)
                                          - m.get("eval_before_cp", 0)))
        ms = move_slots(m)
        head = ("%s's %s was the move of the game."
                % (ms["Winner"], ms["move_screen"]))
        head_spk = ("%s's %s was the move of the game."
                    % (ms["Winner"], ms["move_speech"]))
        ach = _achievement_pair(m, teller, ms) if m.get("best_line") else None
        if ach:
            notes.append((head + " " + ach[0], head_spk + " " + ach[1], {}))
        else:
            notes.append((head, head_spk, {}))
    return notes


def _achievement_pair(e, teller, s):
    """What the move won - and what it threatens. Both verifiable (2026-10-23):
    the immediate gain is ON THE BOARD (material_swing); the threat is the
    continuation shown under the text box, cited with its move number."""
    # what the move itself captured
    swing = e.get("material_swing") or 0
    if swing >= 1:
        what = ("a pawn" if swing < 3 else "a piece" if swing < 5 else
                "the exchange" if swing < 8 else "the queen")
        scr, spk = teller.rng.choice(ACHIEVE_WIN)
        d = dict(s, what=what)
        return (scr.format(**d), spk.format(**d), {})
    # what the move threatens: the outcome of the SHOWN prediction line
    nl = e.get("next_line") or {}
    c = nl.get("concl") or {}
    if c.get("kind") == "mate":
        return teller.rng.choice(ACHIEVE_MATE) + ({},)
    if c.get("kind") == "wins":
        # only name a material threat when the NET gain at the end of the
        # line is more than a pawn: a capture the opponent recaptures is a
        # TRADE, not 'the threat is to win a pawn' (2026-11-04)
        if (nl.get("total") or 0) <= 1:
            return None
        what = c.get("what") or "material"
        at = c.get("at")
        mv = c.get("move") or ""
        scr, spk = teller.rng.choice(THREAT_WIN)
        # only cite the move when the line's own survival rule kept its name
        disp = _next_move_disp(e, at) if mv else ""
        d = dict(s, what=what,
                 **{"with": (" with %s" % disp) if disp else "",
                    "with_spk": (" with %s" % spoken_san(mv)) if mv else ""})
        return (scr.format(**d), spk.format(**d), {})
    w = wdl_phrase(e.get("wdl"))
    if w:
        # only say the outcome when it is decisive or clearly changed hands -
        # repeating "White is winning" move after move is noise
        wp = _wp(e.get("wdl"), e["color"])
        swung = abs(_wp(e.get("wdl_before"), e["color"]) - wp) >= 20
        if wp >= 75 or swung:
            line = w[0].upper() + w[1:] + "."
            return (line, line, {})
    return None


def _concept_note(teller, e, s):
    """The aspect of the position that changed most on this move - the
    informative core of an otherwise quiet comment (concept deltas come
    from the analysis; material gains are the achievement's job)."""
    deltas = e.get("concepts") or {}
    top_k, top_d = None, 0
    for k, d in deltas.items():
        if k == "material" or k not in CONCEPT_NOTES:
            continue
        if abs(d) >= CONCEPT_MIN.get(k, 2) and abs(d) > abs(top_d):
            top_k, top_d = k, d
    if not top_k:
        return None
    # Harvested human phrasing is OFF (2026-10-23): mined sentences leaked
    # player names ('Lasker (black) controls...') and unverifiable claims
    # ('black has 3 center pawns') - a phrase is only safe when it can be
    # verified against the position, which the library cannot guarantee.
    # The banks below are templated over computed facts instead.
    pool = CONCEPT_NOTES[top_k][0 if top_d > 0 else 1]
    text = teller.rng.choice(pool).format(Color=s["Color"])
    return (text, text, {"kind": "concept", "concept": top_k})


def _swing_pair(teller, e, s):
    """What the engine says changed: winning chances when it reported a real
    swing. (Plain evaluation words read badly in a sentence like 'the
    position goes from a completely winning position to a winning
    advantage' - say nothing instead.)"""
    if (e.get("wdl") and e.get("wdl_before")
            and abs(s["wp_before"] - s["wp_after"]) >= 12):
        t = ("{Color}'s winning chances fall from {wp_before}% to "
             "{wp_after}%." if s["wp_after"] < s["wp_before"] else
             "{Color}'s winning chances rise from {wp_before}% to "
             "{wp_after}%.")
        return (t.format(**s), t.format(**s), {})
    return None


def _next_move_disp(e, at):
    """The prediction line's move with its number - checkable against the
    'Next:' list under the text box (2026-10-23)."""
    plies = e.get("next_plies") or []
    if at is None or at >= len(plies):
        return ""
    p = plies[at]
    return "%d%s%s" % (p.get("no", 0), p.get("dots", ""), p["san"])


def _contrast_pair(e, teller, s):
    """Why the better move was better: what the played move actually won,
    against the recommended line's gain (2026-10-23)."""
    bi = e.get("best_impact") or {}
    if bi.get("kind") != "wins" or bi.get("what") == "the game":
        return None
    swing = e.get("material_swing") or 0
    if swing <= 0:
        text = "The move played wins nothing."
        return (text, text, {})
    won = ("a pawn" if swing < 3 else
           "a piece" if swing < 5 else
           "the exchange" if swing < 8 else "the queen")
    if won == bi.get("what"):
        return None
    text = "The move played wins only %s." % won
    return (text, text, {})


def _reply_threat(e, teller, s):
    """What the opponent is ready to do next - only from the prediction line
    shown on screen, and saying WHAT the tactic is (2026-10-24): a pin names
    the pinned piece and its target, a fork names both pieces. The move
    itself is the source - 'White threatens 6.Bg5, pinning the knight on f6
    to the queen'."""
    plies = e.get("next_plies") or []
    if not plies:
        return None
    p = plies[0]
    if p.get("color") == e["color"]:
        return None                    # should not happen - be safe
    parts = []
    if p["san"].endswith("#"):
        # checkmate says it all - a fork on the mating move helps nobody
        text = "%s threatens checkmate with %d%s%s." % (
            s["opponent"], p["no"], p.get("dots", ""), p["san"])
        spk = "%s threatens checkmate with %s." % (s["opponent"],
                                                   spoken_san(p["san"]))
        return (text, spk, {})
    if p.get("check") and not p["san"].endswith(("+", "#")):
        # the SAN already says 'with check' / 'checkmate' when marked
        parts.append("giving check")
    for f in (p.get("facts") or []):
        if f.get("kind") == "pin":
            parts.append("pinning the %s on %s to the %s"
                         % (f.get("piece", "piece"), f.get("square", "?"),
                            f.get("target", "piece")))
        elif f.get("kind") == "fork":
            parts.append("forking the %s and the %s"
                         % (f.get("first", "pieces"),
                            f.get("second", "pieces")))
    if not parts:
        return None
    move_disp = "%d%s%s" % (p["no"], p.get("dots", ""), p["san"])
    tail = ", " + ", ".join(parts)
    text = "%s threatens %s%s." % (s["opponent"], move_disp, tail)
    spk = "%s threatens %s%s." % (s["opponent"], spoken_san(p["san"]),
                                  tail)
    return (text, spk, {})


def _alternatives_pair(e, teller, s):
    """The other moves the engine considered - 'the things to consider'.
    Only when the player deviated from the engine's choice: for the engine's
    own move the prediction line already shows what comes next."""
    if e["uci"] == e.get("best"):
        return None
    cand = [c for c in (e.get("candidates") or [])
            if c.get("san") and c.get("san") != e["san"]]
    seen, opts = set(), []
    for c in cand:
        if c["san"] not in seen:
            seen.add(c["san"])
            opts.append(c["san"])
    opts = opts[:2]
    if len(opts) < 2:
        return None
    text = "Other options were %s and %s." % (opts[0], opts[1])
    spk = "Other options were %s and %s." % (spoken_san(opts[0]),
                                             spoken_san(opts[1]))
    return (text, spk, {})


def _fact_note(teller, facts, slots):
    """One positional note (screen tpl, speech tpl, slots): the most
    important fact present."""
    kinds = {f["kind"] for f in facts}
    for k in FACT_PRIORITY:
        if k in kinds:
            f = next(x for x in facts if x["kind"] == k)
            pair = teller.fact(k)
            if pair:
                return (pair[0], pair[1], dict(slots, **f))
    return None


def _sent_case(s):
    return s[0].upper() + s[1:] if s else s


def _opening_name(analysis):
    ops = analysis.get("openings") or []
    if ops:
        return ops[-1]["name"]
    ucis = [m["uci"] for m in analysis["moves"][:2]]
    for seq, name in OPENINGS:
        if ucis[:len(seq)] == seq:
            return name
    return "an irregular opening"


def _result_line(meta, analysis):
    res = meta.get("result", "*")
    last = analysis["moves"][-1] if analysis["moves"] else None
    if last and last.get("mate_after") == 1:
        winner = "White" if last["color"] == "white" else "Black"
        return "%s mates on move %d." % (winner, last["no"])
    if res == "1-0":
        return "White wins on move %d." % analysis["moves"][-1]["no"]
    if res == "0-1":
        return "Black wins on move %d." % analysis["moves"][-1]["no"]
    if res == "1/2-1/2":
        return "The game is drawn."
    return "The game ends here."


def _game_arc(analysis):
    """How the game went, from the evaluation trail: 'dominant' (one side led
    throughout), 'close' (never settled), 'swingy' (the lead changed hands),
    'comeback' (the winner was worse before turning it)."""
    moves = analysis["moves"]
    res = analysis["meta"].get("result")
    winner = ("white" if res == "1-0" else "black" if res == "0-1" else None)
    if not moves or winner is None:
        return "close"
    ws = 1 if winner == "white" else -1
    trail = [m["eval_cp"] * ws for m in moves]
    flips, last = 0, 0
    for v in trail:
        sgn = 1 if v >= 150 else -1 if v <= -150 else 0
        if sgn and last and sgn != last:
            flips += 1
        if sgn:
            last = sgn
    late = trail[8:] or trail
    dominated = sum(1 for v in late if v >= 250) / float(len(late))
    was_behind = any(v <= -250 for v in trail[10:])
    if was_behind and flips < 2:
        return "comeback"
    if flips >= 2:
        return "swingy"
    if dominated >= 0.6:
        return "dominant"
    return "close"


def build_events(analysis, teller):
    meta = analysis["meta"]
    events = []
    slots = {
        "White": meta.get("white", "White"),
        "Black": meta.get("black", "Black"),
        "event_year": ", ".join(x for x in (meta.get("event", ""),
                                            meta.get("date", "")[:4]) if x)
                      or "the board",
        "opening": _opening_name(analysis),
        "opening_cap": _sent_case(_opening_name(analysis)),
    }
    events.append({"kind": "intro", "ply": 0,
                   "pack": teller.pick("intro", 2), "slots": dict(slots)})

    opening_map = {}
    for op in analysis.get("openings", []):
        opening_map.setdefault(op["ply"], []).append(op["name"])
    phase_map = {}
    for ph in analysis.get("phases", []):
        phase_map.setdefault(ph["ply"], []).append(ph)
    player = (analysis.get("meta") or {}).get("player")

    for e in analysis["moves"]:
        q = e["quality"]
        facts = e.get("facts") or []
        color = e["color"]
        opp = "black" if color == "white" else "white"
        no = e["no"]
        dots = "." if color == "white" else "..."
        s = dict(slots)
        s.update({
            "color": color, "Color": color.capitalize(),
            "opponent": opp.capitalize(),
            "move_screen": "%d%s%s" % (no, dots, e["san"]),
            "move_speech": spoken_san(e["san"]),
            "best_screen": "%d%s%s" % (no, dots, e["best_san"]),
            "best_speech": spoken_san(e["best_san"]),
            "eval_before": AN._eval_words(e["eval_before_cp"]),
            "eval_after": AN._eval_words(e["eval_cp"]),
            "swing": abs(e["eval_cp"] - e["eval_before_cp"]),
            "wp_before": _wp(e.get("wdl_before"), e["color"]),
            "wp_after": _wp(e.get("wdl"), e["color"]),
            "gain": ((e["eval_cp"] - e["eval_before_cp"])
                     * (1 if color == "white" else -1)),
            "what": ("the queen" if e["material_swing"] <= -8 else
                     "the exchange" if e["material_swing"] <= -3 else
                     "a pawn" if e["material_swing"] < 0 else "the advantage"),
        })
        mover_eval = e["eval_before_cp"] * (1 if color == "white" else -1)
        bank = (IMPACT_GOOD if mover_eval >= 80 else
                IMPACT_LOST if mover_eval <= -250 else
                IMPACT_HOLD if mover_eval <= -80 else
                IMPACT_NEUTRAL)
        # the impact of the RECOMMENDED line comes from the line itself when
        # it wins something concrete; the phrase bank covers the rest
        bi = e.get("best_impact") or {}
        if bi.get("kind") == "wins":
            s["impact"] = ("finished the game" if bi.get("what") == "the game"
                           else "won %s" % bi["what"])
        else:
            s["impact"] = teller.phrase(bank).format(**s)

        # opening callout at the move that defines it (no move head of its
        # own - the move's own event carries the line)
        for op in opening_map.get(e["ply"], []):
            events.append({"kind": "opening", "move": None, "ply": e["ply"],
                           "pack": teller.pick("opening", 1),
                           "slots": dict(s, opening=op,
                                         opening_cap=_sent_case(op))})

        # high-level analysis at the phase transitions
        for ph in phase_map.get(e["ply"], []):
            events.append({"kind": "phase", "move": None, "ply": e["ply"],
                           "pack": (),
                           "extras": [_phase_note(teller,
                                                  dict(ph,
                                                       eval_cp=e["eval_cp"]),
                                                  s)],
                           "slots": s})

        if (analysis["moves"][-1].get("mate_after") == 1
                and e["ply"] == analysis["moves"][-1]["ply"]):
            # the mating move is the conclusion, not a move to review
            events.append({"kind": "finish", "move": e, "ply": e["ply"],
                           "pack": (), "slots": s})
            continue

        # ── the comment: facts only (2026-10-23) ──────────────────────────
        # Every sentence must be traceable to the engine or the board: a
        # brief quality verdict on each move, the engine's own line on the
        # mistakes, what the line achieves on the good ones. Quiet stretches
        # get NO commentary - the earlier free-text approach asserted things
        # the board could not back up (player names, wrong pawn counts,
        # 'nothing critical happens for 5 moves').
        pack = list(teller.pick("verdict_" + q, 1))
        extras = []
        line_pair = None
        if q in ("inaccuracy", "mistake", "blunder"):
            # the recommended line: [a better alternative would be] [move
            # series] [impact] - mostly the player's own moves (5/6). Only
            # when the engine's choice differs from what was played - when
            # it agrees, the damage is what the move allows, not a missed
            # alternative.
            want_line = (e["loss_cp"] >= 90 and bool(e.get("best_line"))
                         and e["uci"] != e.get("best"))
            if want_line:
                p = (0.9 if (player and e["color"] == player) else
                     0.18 if player else 0.8)
                want_line = teller.rng.random() < p
            line_pair = _line_pair(teller, e, s) if want_line else None
            if line_pair:
                extras.append(line_pair)
                # why the better move was better: the outcomes, side by side
                cw = _contrast_pair(e, teller, s)
                if cw:
                    extras.append(cw)
            if e.get("mate_after") is not None and e["mate_after"] < 0:
                extras.append(("{Color} allows a forced mate.",
                               "{Color} allows a forced mate.", s))
            else:
                sw = _swing_pair(teller, e, s)
                if sw and (not line_pair or e["loss_cp"] >= 250):
                    extras.append(sw)
        elif e["uci"] == e.get("best"):
            # what the move achieves, straight from the engine's own line
            ach = _achievement_pair(e, teller, s)
            if ach:
                extras.append(ach)
        # what the opponent is ready to do next - straight from the
        # prediction line shown on screen (2026-10-23)
        if len(extras) < 2 and not line_pair:
            th = _reply_threat(e, teller, s)
            if th:
                extras.append(th)
        # the other moves the engine considered - only where the verdict is
        # itself negative: under 'A mistake' the list reads as 'what to have
        # considered', while under 'A good move' it second-guesses a move the
        # analysis just approved (2026-10-24)
        if not extras and q in ("inaccuracy", "mistake", "blunder"):
            al = _alternatives_pair(e, teller, s)
            if al:
                extras.append(al)
        # a move that gives check says so in the text (the spoken move
        # already says 'with check', so the speech skips this) - 2026-10-23
        if e.get("check") and not e.get("mate_after"):
            said = " ".join(x[0] for x in extras) + " " + " ".join(pack)
            if "check" not in said.lower() and "+" not in said:
                extras.append(("That gives check.", "", {}))
        events.append({"kind": q, "move": e, "ply": e["ply"], "pack": pack,
                       "extras": extras, "slots": s})

    # the move the game turned on: the loser's costliest (same move the
    # closing note names - the outro sentence and the note must agree)
    loser_side = ("black" if meta.get("result") == "1-0" else
                  "white" if meta.get("result") == "0-1" else None)
    cand = [m for m in analysis["moves"]
            if loser_side and m["color"] == loser_side and m["loss_cp"] >= 150]
    cand = cand or analysis["moves"]
    turn_no = max(cand, key=lambda m: m["loss_cp"])["no"] if cand else 1
    arc = _game_arc(analysis)
    last = analysis["moves"][-1] if analysis["moves"] else None
    mate_end = bool(last and last.get("mate_after") == 1)
    res = meta.get("result")
    out_slots = dict(slots)
    out_slots.update({
        "result_line": _result_line(meta, analysis),
        "turn_no": turn_no,
        "winner": ("White" if res == "1-0" else
                   "Black" if res == "0-1" else "White"),
        "final_screen": ("%d%s%s" % (last["no"],
                                     "." if last["color"] == "white" else "...",
                                     last["san"]) if last else ""),
        "final_speech": (spoken_san(last["san"]) if last else ""),
    })
    notes = _closing_notes(analysis, teller, out_slots)
    events.append({"kind": "outro", "ply": None,
                   "pack": teller.pick("outro_%s%s"
                                       % (arc, "_mate" if mate_end else ""), 3),
                   "extras": [(n[0], n[1], n[2]) for n in notes],
                   "slots": out_slots})
    return events


def _slots_for(ev, style):
    sd = dict(ev["slots"])
    if "best_screen" in sd:
        sd["best"] = (sd["best_screen"] if style == "screen"
                      else sd["best_speech"])
        sd["Best"] = (sd["best_screen"] if style == "screen"
                      else sd["best_speech"].capitalize())
    if "final_screen" in sd:
        sd["final"] = (sd["final_screen"] if style == "screen"
                       else sd["final_speech"])
    if "move_screen" in sd:
        sd["move"] = (sd["move_screen"] if style == "screen"
                      else sd["move_speech"])
    return sd


def _body(ev, style, skip_pack=False):
    sd = _slots_for(ev, style)
    body = "" if skip_pack else " ".join(t.format(**sd) for t in ev["pack"])
    for pair in ev.get("extras", []):
        ex = pair[0] if style == "screen" else pair[1]
        body = (body + " " + ex.format(**pair[2])).strip()
    return body


def _head(ev, style):
    """The move heading for one event ('' when the event has no move)."""
    move = ev.get("move")
    if move is None:
        return ""
    if ev["kind"] == "finish":
        if style == "screen":
            no, dots = move["no"], ("." if move["color"] == "white" else "...")
            return "%d%s%s%s" % (no, dots, move["san"],
                                 mark_for(move["quality"]))
        # the spoken move SAYS checkmate (2026-10-23) - it used to be stripped
        return spoken_san(move["san"]).capitalize() + "!"
    if style == "screen":
        no, dots = move["no"], ("." if move["color"] == "white" else "...")
        return "%d%s%s%s" % (no, dots, move["san"],
                             mark_for(move["quality"]))
    spk = spoken_san(move["san"]).capitalize()
    if move["quality"] == "brilliant":
        spk += "!"
    return spk


def render(events, analysis, style, skip_pack=False):
    """style: 'screen' (numbers + notation) or 'speech' (spoken, no numbers)."""
    lines = []
    for ev in events:
        body = _body(ev, style, skip_pack=skip_pack)
        head = _head(ev, style)
        if not head:
            lines.append(body)
        elif ev["kind"] == "finish":
            lines.append(head)
        elif style == "screen":
            lines.append("%-12s %s" % (head, body))
        else:
            sep = " " if head.endswith("!") else ". "
            lines.append(head + sep + body if body else head)
    return "\n".join(lines)


PIECE_GLYPH = {"k": "\u265a", "q": "\u265b", "r": "\u265c", "b": "\u265d",
               "n": "\u265e", "p": "\u265f"}


def glyph_of(san):
    """The moving piece's glyph for the move line."""
    if san.startswith("O"):
        return PIECE_GLYPH["k"]
    c = san[:1]
    return PIECE_GLYPH.get(c.lower() if c.isupper() else "p", "")


QUALITY_KINDS = ("best", "good", "great", "brilliant",
                 "inaccuracy", "mistake", "blunder")


def per_move(analysis, seed=None):
    """Per-move packages for the Analysis screens: the quality verdict (the
    LABEL above the text box), the comment as segments (icon / bold move /
    plain text) and the spoken text for the audio. In the opening phase the
    speech leaves the verdict out - the openings and named structures speak
    instead (2026-10-23)."""
    teller = Teller(seed)
    events = build_events(analysis, teller)
    by = {}
    for ev in events:
        if ev.get("ply") is not None:
            by.setdefault(ev["ply"], []).append(ev)
    # the opening phase ends at the first transition to the middlegame
    opening_until = 0
    for ph in (analysis.get("phases") or []):
        if ph.get("to") == "middlegame":
            opening_until = ph.get("ply") or 0
            break
    out = []
    for m in analysis["moves"]:
        segs, spk = [], []
        has_head = False
        verdict = ""
        # the move's own comment renders first; intro prefaced, quiet/opening/
        # phase transition notes trail it (2026-10-22)
        evs = sorted(by.get(m["ply"], []),
                     key=lambda e: (0 if e["kind"] == "intro" else
                                    1 if e["kind"] not in
                                    ("intro", "outro", "quiet", "opening",
                                     "phase") else 2))
        for ev in evs:
            quality_ev = ev["kind"] in QUALITY_KINDS
            if quality_ev and not verdict and ev.get("pack"):
                verdict = " ".join(t.format(**_slots_for(ev, "screen"))
                                   for t in ev["pack"])
            body_s = _body(ev, "screen", skip_pack=quality_ev)
            if ev["kind"] in ("intro", "outro", "quiet", "opening", "phase"):
                if body_s:
                    segs.append(("text", body_s))
                # body only: the move line itself comes from the head insert
                # below (render() would say the move twice)
                body_sp = _body(ev, "speech")
                if body_sp:
                    spk.append(body_sp)
                continue
            head_s = _head(ev, "screen")
            if head_s:
                segs.append(("icon", glyph_of(m["san"])))
                segs.append(("move", head_s))
                has_head = True
                if body_s:
                    segs.append(("text", " " + body_s))
            elif body_s:
                segs.append(("text", body_s))
            # the verdict lives in the LABEL on screen; the speech says it
            # only when the engine attributes a SIGNIFICANT positional change
            # to the move (2026-11-04) - 'the best move' over a quiet move is
            # noise - and never in the opening phase
            swing = abs((m.get("eval_cp") or 0)
                        - (m.get("eval_before_cp") or 0))
            sig = (m.get("quality") in ("great", "brilliant", "inaccuracy",
                                        "mistake", "blunder") or swing >= 80)
            say_verdict = not (quality_ev and
                               (m["ply"] < opening_until or not sig))
            body_sp = _body(ev, "speech", skip_pack=not say_verdict)
            head_spk = _head(ev, "speech")
            if ev["kind"] == "finish":
                line = head_spk
            elif body_sp and not head_spk.endswith("!") \
                    and teller.rng.random() < 0.35:
                # vary when the move is mentioned: 'with [move]' ends the
                # first sentence instead of the move leading it
                first, _dot, rest = body_sp.partition(". ")
                first = first.rstrip(".").strip()
                if " with " in first:
                    # the sentence already names a move - leave the move in
                    # front rather than say 'with' twice
                    line = head_spk + ". " + body_sp
                else:
                    line = "%s with %s." % (first, spoken_san(m["san"]))
                    if rest:
                        line += " " + rest
            elif body_sp:
                line = head_spk + (" " if head_spk.endswith("!") else ". ") \
                    + body_sp
            else:
                # the invisible period: unseen on screen, but the voice must
                # hear the sentence end (2026-10-23)
                line = (head_spk if head_spk.endswith((".", "!", "?"))
                        else head_spk + ".")
            if line:
                spk.append(line)
        if not has_head:
            # every move shows its line: icon + bold notation
            segs.insert(0, ("move", "%d%s%s" % (m["no"],
                                                "." if m["color"] == "white"
                                                else "...", m["san"])))
            segs.insert(0, ("icon", glyph_of(m["san"])))
            spk.insert(0, spoken_san(m["san"]).capitalize() + ".")
        # segments concatenate literally: text after a move or text needs space
        for i in range(1, len(segs)):
            if (segs[i][0] == "text" and segs[i - 1][0] in ("move", "text")
                    and not segs[i][1].startswith(" ")):
                segs[i] = ("text", " " + segs[i][1])
        speech = " ".join(spk)
        out.append({"ply": m["ply"], "no": m["no"], "color": m["color"],
                    "san": m["san"], "uci": m["uci"], "segments": segs,
                    "verdict": verdict, "next": m.get("next", ""),
                    "next_uci": m.get("next_uci", []),
                    "next_plies": m.get("next_plies", []),
                    "best_uci": m.get("best_uci", []),
                    "best_line": m.get("best_line") or {},
                    "wdl": m.get("wdl"), "quality": m.get("quality", ""),
                    "best": m.get("best"),
                    "eval_cp": m.get("eval_cp", 0),
                    "eval_before_cp": m.get("eval_before_cp", 0),
                    "speech": speech, "words": len(speech.split())})
    return out


def narrate(analysis, seed=None, style="both"):
    teller = Teller(seed)
    events = build_events(analysis, teller)
    out = {"seed": teller.seed}
    if style in ("both", "screen"):
        out["screen"] = render(events, analysis, "screen")
    if style in ("both", "speech"):
        out["speech"] = render(events, analysis, "speech")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="narrate a PGN game")
    ap.add_argument("pgn")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--style", choices=("both", "screen", "speech"),
                    default="both")
    ap.add_argument("--depth", type=int, default=15)
    ap.add_argument("--cached", action="store_true",
                    help="re-tell from the cached analysis (no engine run)")
    a = ap.parse_args(argv)

    if a.cached:
        import json
        analysis = json.load(open(AN.cache_path()))
    else:
        analysis = AN.analyze_pgn(a.pgn, depth=a.depth,
                                  progress=lambda i, n: print("  analyzing "
                                                              "%d/%d" % (i, n),
                                                              end="\r"))
    print()
    out = narrate(analysis, seed=a.seed, style=a.style)
    print("=== SCREEN (seed %s) ===" % out["seed"])
    if "screen" in out:
        print(out["screen"])
    if "speech" in out:
        print()
        print("=== SPEECH ===")
        print(out["speech"])
    path = os.path.join(ROOT, "data", "narrative_last.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for k in ("screen", "speech"):
            if k in out:
                f.write("=== %s ===\n%s\n\n" % (k.upper(), out[k]))
    AN.save_analysis(analysis)
    print("\nsaved:", path)
    return out


if __name__ == "__main__":
    main()
