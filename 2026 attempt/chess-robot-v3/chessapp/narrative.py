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
import os
import random

from chessapp import analysis as AN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    ("{White} against {Black} - {event_year}.",),
    ("This is {White} versus {Black}, {event_year}.",),
    ("{event_year}: {White} has White, {Black} is Black.",),
    ("A game between {White} and {Black}, {event_year}.",),
    ("On the board: {White} versus {Black}.",),
    ("This is {White} against {Black}.", "It opens as {opening}."),
    ("{White} versus {Black} - {event_year}.", "{opening_cap}, so "
     "anything can happen."),
    ("Here is {White} against {Black}.", "It opens as {opening}."),
]

QUIET = [
    ("The next {n} moves are quiet manoeuvring.",),
    ("Both sides spend {n} moves improving their position.",),
    ("{n} moves of careful development follow.",),
    ("Nothing critical happens for {n} moves.",),
    ("A stretch of shuffling follows - {n} moves without a capture.",),
    ("The players take {n} moves to rearrange their pieces.",),
    ("For {n} moves, it is a waiting game.",),
]

PRAISE_QUIET = [
    ("Solid.",),
    ("Sensible.",),
    ("A natural move.",),
    ("Developing calmly.",),
    ("Nothing wrong with that.",),
    ("Steady play.",),
]

PRAISE = [
    ("The best move.",),
    ("The best move in the position.",),
    ("The best move - nothing else comes close.",),
    ("Exactly the best move.",),
    ("Playing the best move.",),
    ("The right move, and the best one.",),
]

GREAT = [
    ("An excellent move!",),
    ("A strong choice.", "{Color} keeps every threat alive."),
    ("This is the move the position was asking for.",),
    ("The clearest path in the position.",),
    ("Nicely played.", "{Color} keeps the initiative."),
    ("Very strong.", "The attack rolls on."),
    ("A sharp move!", "It is hard to answer."),
    ("The best move by some distance.",),
    ("This keeps the pressure on.", "Nothing is handed back."),
    ("Well played!", "{Color} finds the point of the position."),
]

BRILLIANT = [
    ("A brilliant move!",),
    ("A brilliant move!", "{Color} has seen further than the opponent."),
    ("This ends the game by force.",),
    ("The finish is forced from here.",),
    ("A lovely move!", "The attack lands with force."),
    ("The best move by a distance - and it finishes the game.",),
    ("Beautifully played!", "{Color} converts with precision."),
    ("A brilliant finish!",),
]

INACCURACY = [
    ("A small imprecision.",),
    ("Playable.", "{Color} could have played {best}, which would have "
     "{impact}."),
    ("A slight loosening.", "{Best} was the accurate move - that would have "
     "{impact}."),
    ("This gives a little ground.", "{Best} was better: that would have "
     "{impact}."),
    ("Not the cleanest choice.", "{Color} could have gone with {best}, which "
     "would have {impact}."),
    ("The position tolerates it.", "{best} was stronger - that would have "
     "{impact}."),
    ("A quieter alternative was better.", "{Best} would have {impact}."),
]

MISTAKE = [
    ("A mistake under pressure.", "{Color} could have played {best}, which "
     "would have {impact}."),
    ("This lets {opponent} in.", "The right move was {best} - that would have "
     "{impact}."),
    ("The initiative changes hands here.", "{Color} should have chosen "
     "{best}, which would have {impact}."),
    ("This gives up the advantage.", "Better was {best}; that would have "
     "{impact}."),
    ("An inaccuracy that costs real ground.", "With {best}, that would have "
     "{impact}."),
    ("This is not the accurate move.", "{Best} was the way to go - that "
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
    ("The decisive mistake.", "{Best} was the move - it would have "
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
        ("A strong game by {winner} - the finish was never in doubt.",),
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
         "A fork - the {piece} attacks the {first} and the {second} at the "
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
         "The {piece} on {square} is tied down - moving it would expose "
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
    ("{opening_cap} - {move} defines it.",),
]

TEMPLATES = {"intro": INTRO, "quiet": QUIET, "praise": PRAISE,
             "praise_quiet": PRAISE_QUIET, "great": GREAT,
             "brilliant": BRILLIANT, "inaccuracy": INACCURACY,
             "mistake": MISTAKE, "blunder": BLUNDER,
             "blunder_swing": BLUNDER_SWING,
             "turning_good": TURNING_GOOD, "turning_bad": TURNING_BAD,
             "decision": DECISION, "finish": FINISH,
             "opening": OPENING_LINES}
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

    def phrase(self, bank):
        return self.rng.choice(bank)


# ── narrative assembly ───────────────────────────────────────────────────────

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
    events.append({"kind": "intro", "pack": teller.pick("intro", 2),
                   "slots": dict(slots)})

    quiet_map = {}
    covered = set()
    for run in analysis.get("quiet_runs", []):
        quiet_map[run[0]] = len(range(run[0], run[1] + 1))
        covered.update(range(run[0], run[1] + 1))
    opening_map = {}
    for op in analysis.get("openings", []):
        opening_map.setdefault(op["ply"], []).append(op["name"])

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

        # opening callout at the move that defines it
        for op in opening_map.get(e["ply"], []):
            events.append({"kind": "opening", "move": e,
                           "pack": teller.pick("opening", 1),
                           "slots": dict(s, opening=op,
                                         opening_cap=_sent_case(op))})

        if e["ply"] in covered:
            if e["ply"] in quiet_map:      # one bridge per quiet stretch
                events.append({"kind": "quiet", "move": None,
                               "pack": teller.pick("quiet", 1),
                               "slots": dict(s, n=quiet_map[e["ply"]])})
            continue

        if (analysis["moves"][-1].get("mate_after") == 1
                and e["ply"] == analysis["moves"][-1]["ply"]):
            # the mating move is the conclusion, not a move to review
            events.append({"kind": "finish", "move": e, "pack": (),
                           "slots": s})
            continue

        tier = {"brilliant": 3, "blunder": 3, "mistake": 2, "great": 2,
                "inaccuracy": 2}.get(q, 1)
        say = q in ("brilliant", "great", "inaccuracy", "mistake", "blunder")
        if q in ("best", "good") and not e.get("forced"):
            if teller.rng.random() < 0.12:
                say, tier = True, 1
        if not say:
            # an occasional stand-alone positional note on a notable move
            if facts and teller.rng.random() < (
                    0.3 if (e.get("capture") or e.get("check")) else 0.1):
                ex = _fact_note(teller, facts, s)
                if ex:
                    events.append({"kind": "fact", "move": e, "pack": (),
                                   "extras": [ex], "slots": s})
            continue

        cat = {"brilliant": "brilliant", "great": "great",
               "inaccuracy": "inaccuracy", "mistake": "mistake"}.get(q)
        if cat is None and q == "blunder":
            # the before/after line only earns its place on a real swing
            cat = ("blunder_swing"
                   if s["swing"] >= 300 and s["eval_before"] != s["eval_after"]
                   else "blunder")
        if cat is None:
            cat = ("praise_quiet" if e["quiet"] else "praise")
        pack = list(teller.pick(cat, TIER_LIMIT[tier]))

        # one extra sentence for story beats (never on the finish itself)
        beat = e["ply"] in analysis.get("turning_points", []) and \
            q != "brilliant"
        if beat and teller.rng.random() < 0.6:
            good = s["gain"] >= 0          # from the MOVER's side
            pack += teller.pick("turning_good" if good else "turning_bad", 1)
        elif (e["ply"] in analysis.get("decision_points", [])
              and cat in ("praise", "praise_quiet", "inaccuracy")
              and teller.rng.random() < 0.25):
            pack += teller.pick("decision", 1)

        # positional note - moderately often, more on important moves; never
        # pile onto a comment that already has plenty to say
        extras = []
        if facts and len(pack) <= 2 and teller.rng.random() < (
                0.75 if tier >= 2 else 0.4):
            ex = _fact_note(teller, facts, s)
            # a good move that deliberately hangs its own piece is a sacrifice
            if (ex and ex[2].get("kind") == "exposes"
                    and q in ("great", "brilliant")
                    and ex[2].get("square") == e["uci"][-2:]):
                pair = teller.fact("sacrifice")
                if pair:
                    ex = (pair[0], pair[1],
                          dict(ex[2], piece=ex[2].get("piece", "piece")))
            if ex:
                extras.append(ex)
        events.append({"kind": q, "move": e, "pack": pack,
                       "extras": extras, "slots": s})

    turn_no = max(analysis["moves"],
                  key=lambda m: m["loss_cp"])["no"] if analysis["moves"] else 1
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
        "final_speech": ((spoken_san(last["san"])
                          .replace(", checkmate", ""))
                         if last else ""),
    })
    events.append({"kind": "outro",
                   "pack": teller.pick("outro_%s%s"
                                       % (arc, "_mate" if mate_end else ""), 3),
                   "slots": out_slots})
    return events


def render(events, analysis, style):
    """style: 'screen' (numbers + notation) or 'speech' (spoken, no numbers)."""
    lines = []
    for ev in events:
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
        body = " ".join(t.format(**sd) for t in ev["pack"])
        for pair in ev.get("extras", []):
            ex = pair[0] if style == "screen" else pair[1]
            body = (body + " " + ex.format(**pair[2])).strip()
        move = ev.get("move")
        if ev["kind"] in ("intro", "outro", "quiet", "opening"):
            lines.append(body)
            continue
        if ev["kind"] == "finish":
            # bare move: the conclusion that follows tells the story
            if style == "screen":
                no, dots = move["no"], ("." if move["color"] == "white"
                                        else "...")
                lines.append("%d%s%s%s" % (no, dots, move["san"],
                                           mark_for(move["quality"])))
            else:
                spk = spoken_san(move["san"])
                spk = spk.replace(", checkmate", "")   # the conclusion says it
                lines.append(spk.capitalize() + "!")
            continue
        if style == "screen":
            no, dots = move["no"], ("." if move["color"] == "white" else "...")
            head = "%d%s%s%s" % (no, dots, move["san"],
                                 mark_for(move["quality"]))
            lines.append("%-12s %s" % (head, body))
        else:
            spk = spoken_san(move["san"]).capitalize()
            if move["quality"] == "brilliant":
                spk += "!"
            sep = " " if spk.endswith("!") else ". "
            lines.append(spk + sep + body)
    return "\n".join(lines)


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
