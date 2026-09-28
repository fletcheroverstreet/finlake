"""Financial sentiment lexicon.

**Why not a general-purpose sentiment model.** Ordinary sentiment lexicons are
badly wrong on financial text, and wrong in a consistent direction. Loughran
and McDonald's 2011 study found that roughly three quarters of the words a
standard psychological dictionary flags as negative are not negative in a
financial filing at all: *liability*, *cost*, *tax*, *capital*, *crude*,
*depreciation*, *foreign* are neutral accounting vocabulary. Score a 10-K with
a general lexicon and you mostly measure how much accounting it contains.

The word lists below are derived from the Loughran-McDonald financial
sentiment dictionary, the standard in the accounting and finance literature.
This is a **curated subset**, not the full ~4,000-term dictionary — weighted
toward the vocabulary that actually appears in headlines and article summaries,
which is what this scores. That limitation is stated in the UI rather than
implied away.

To use the complete dictionary, drop the LM master CSV somewhere and point
`FINLAKE_LM_DICTIONARY` at it; `load_dictionary()` picks it up and the
bundled lists become the fallback.

**What this is not.** It is a word-counting lexicon, not a language model. It
does not understand sarcasm, context, or whether a "record loss" is bad for
the company or good for its competitor. It is a cheap, transparent,
reproducible signal — every score can be traced to the exact words that
produced it — and it is labelled as such everywhere it surfaces.

WHAT CHANGED, AND WHY THE OLD SCORES READ WRONG.

*One word decided the whole headline.* Tone was `(pos - neg) / (pos + neg)`,
the standard LM normalisation — which is designed for a 10-K, where hundreds
of sentiment words average out. A headline is eight words long and usually
contains exactly one of them, so the formula returned +1.00 or -1.00 and
nothing in between: 85% of every scored article in the cache sat on one of the
two extremes. A single incidental word was carrying a maximum-confidence
verdict. Tone is now scaled by how much evidence produced it, so one word is a
weak reading and six agreeing words is a strong one. The raw LM polarity is
still recoverable from `positive` and `negative`, which are stored beside it.

*Words that are not opinions were counted as opinions.* "Outstanding" was
positive, and "shares outstanding" appears in a large fraction of all
headlines. "Record" was positive, in "record loss". "Fine" was negative, as an
ordinary adjective. "Advance" was positive, inside "Advanced Micro Devices".
The lists are curated against real headline text below, and each removal says
what it was matching.

*Financial writing has idioms that word counts read backwards.* "Failed to
beat estimates" contains a positive word. "Cuts costs" contains a negative
one. `PHRASES` catches the common ones directly, and outranks the word count
because an idiom is unambiguous where its parts are not.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Words REMOVED from the lists below, and what each was actually matching.
#
# Kept as an explicit record rather than silently deleted, because every one
# of them is a plausible-looking entry that somebody will want to add back.
# They were removed after reading what they hit in 67,000 cached headlines.
#
#   negative:
#     departure   "Item 5.02: Departure of Directors" — the single most
#                 common 8-K item, routine board and compensation
#                 boilerplate. On its own it scored ~19,600 filings at -1.00
#                 apiece, at the highest source weight in the system, and was
#                 the largest single reason news read negative.
#     issue(s)    "issues statement", "issues guidance", "issues $2bn notes"
#     cut(s)      "cuts costs", "Fed cuts rates" — direction depends entirely
#                 on the object. Handled in PHRASES where it is decidable.
#     reduce(d)   same: "reduced debt" is good news.
#     fine        an ordinary adjective far more often than a penalty.
#     exit        "exit strategy", "exits position" — routine.
#     critical    "critical component", "mission-critical".
#     serious     rare as a financial judgement, common as filler.
#     correction  "correction: an earlier version of this story..."
#     bear        matches company names and "bear in mind"; `bearish` kept.
#     expensive   a valuation opinion, not an event.
#     restructur* a reorganisation is as often a fix as a symptom.
#     settle*     settling litigation removes an overhang as often as it
#                 confirms one.
#     sanction(ed) "sanctioned" also means authorised.
#     pressure    kept only as the phrase "under pressure".
#
#   positive:
#     outstanding "shares outstanding", "amounts outstanding" — structural
#                 vocabulary, in a large fraction of all filing headlines.
#     record      "record loss", "record outflows", "on record".
#     high(s)     "high costs", "highs and lows", "high-yield".
#     leading     "leading provider" is in every company boilerplate line.
#     leader      same.
#     advance(d)  "Advanced Micro Devices", "advanced manufacturing".
#     positive*   almost always "positive free cash flow" style qualifiers
#                 already counted through their subject.
#     premier / superior / excellent / exceptional
#                 marketing adjectives from the company's own press release,
#                 which is the one voice that is positive regardless of news.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Negative. The largest list, because financial writing has far more ways to
# say something went wrong than something went right.
# ---------------------------------------------------------------------------
NEGATIVE = {
    # Performance
    "loss", "losses", "lost", "losing", "decline", "declines", "declined",
    "declining", "decrease", "decreased", "decreasing", "drop", "drops",
    "dropped", "fall", "falls", "fell", "falling", "plunge", "plunged",
    "plunges", "slump", "slumped", "tumble", "tumbled", "tumbles", "sink",
    "sank", "sinks", "slide", "slid", "slides", "weak", "weaker", "weakness",
    "weakened", "soft", "softer", "softness", "sluggish", "shortfall",
    "miss", "missed", "misses", "missing", "disappoint", "disappointed",
    "disappointing", "disappointment", "underperform", "underperformed",
    "underperformance", "deteriorate", "deteriorated", "deterioration",
    "worsen", "worsened", "worsening", "erode", "eroded", "erosion",
    "contraction", "contracted", "downturn", "slowdown", "slowing", "slowed",
    "stagnant", "stagnation", "recession", "recessionary", "headwind",
    "headwinds", "pressured", "depressed", "subdued",
    # Failure and distress
    "fail", "failed", "failing", "failure", "failures", "bankrupt",
    "bankruptcy", "insolvency", "insolvent", "default", "defaulted",
    "defaults", "delinquent", "delinquency", "distress", "distressed",
    "liquidation", "liquidate", "writedown",
    "writeoff", "write-down", "write-off", "impairment", "impaired",
    "impairments", "downgrade", "downgraded", "downgrades",
    "slash", "slashed", "slashes", "layoff", "layoffs",
    "furlough", "closure", "closures", "shutdown", "halt", "halted",
    "suspend", "suspended", "suspension", "discontinue", "discontinued",
    "recall", "recalled", "recalls", "delay", "delayed", "delays",
    "postpone", "postponed", "cancel", "cancelled", "canceled",
    "terminate", "terminated", "termination", "resign", "resigned",
    "resignation", "ouster", "oust",
    # Legal and regulatory
    "lawsuit", "lawsuits", "sue", "sued", "suing", "litigation", "litigate",
    "investigation", "investigate", "investigated", "probe", "probes",
    "probed", "subpoena", "subpoenaed", "allegation", "allegations",
    "alleged", "allege", "fraud", "fraudulent", "misconduct", "violation",
    "violations", "violated", "penalty", "penalties", "fined",
    "fines", "indictment", "indicted", "guilty", "illegal", "unlawful",
    "breach", "breached", "noncompliance", "antitrust", "monopoly",
    "injunction", "restatement", "restated", "misstatement", "irregularity",
    "irregularities", "whistleblower", "scandal",
    # Risk and uncertainty
    "risky", "concern", "concerns", "concerned", "worry",
    "worries", "worried", "uncertain", "uncertainty", "uncertainties",
    "volatile", "volatility", "warn", "warned", "warning", "warns",
    "caution", "cautious", "cautioned", "threat", "threats", "threaten",
    "threatened", "adverse", "adversely",
    "unfavorable", "unfavourable", "challenging", "challenge", "challenges",
    "difficult", "difficulty", "difficulties", "struggle", "struggled",
    "struggling", "struggles", "trouble", "troubled", "crisis",
    "severe", "damage", "damaged", "damages", "hurt", "harm",
    "harmed", "harmful", "danger", "dangerous", "problem", "problems",
    "defect", "defective", "flaw", "flawed",
    "wrong", "poor", "worse", "worst", "bearish",
    "selloff", "sell-off", "crash", "collapse", "collapsed",
    "plummet", "plummeted", "freefall", "rout", "capitulation",
    "overvalued", "dilution", "dilutive", "burdened",
}

# ---------------------------------------------------------------------------
# Positive. Deliberately shorter. Loughran-McDonald found positive words are
# both rarer and less reliable in financial text — a company describing its
# own results uses them regardless of results — so the asymmetry is real and
# is preserved rather than balanced for neatness.
# ---------------------------------------------------------------------------
POSITIVE = {
    # Performance
    "gain", "gains", "gained", "gaining", "growth", "grow", "grew", "growing",
    "increase", "increased", "increases", "increasing", "rise", "rises",
    "rose", "rising", "climb", "climbed", "climbs", "surge", "surged",
    "surges", "soar", "soared", "soars", "jump", "jumped", "jumps", "rally",
    "rallied", "rallies", "improve",
    "improved", "improving", "improvement", "improvements", "recover",
    "recovered", "recovery", "rebound", "rebounded", "expansion", "expand",
    "expanded", "expanding", "accelerate", "accelerated", "accelerating",
    "acceleration", "momentum", "outperform", "outperformed",
    "outperformance", "beat", "beats", "exceed", "exceeded", "exceeds",
    "exceeding", "surpass", "surpassed", "surpasses", "topped", "tops",
    # Quality and strength
    "strong", "stronger", "strongest", "strength", "strengthen",
    "strengthened", "robust", "solid", "healthy", "resilient", "resilience",
    "stable", "stability", "sustainable", "durable", "efficient",
    "efficiency", "profitable", "profitability", "profit", "profits",
    "lucrative", "successful", "success", "succeed", "succeeded",
    "achievement", "achieve", "achieved", "achieving", "milestone",
    "peak", "best", "leadership",
    "impressive", "remarkable", "favorable", "favourable",
    "benefit", "benefits", "benefited",
    "advantage", "advantages", "opportunity", "opportunities", "attractive",
    "compelling", "confident", "confidence", "optimistic", "optimism",
    "bullish", "upbeat", "encouraging", "promising", "upside", "undervalued",
    # Corporate actions read as positive
    "upgrade", "upgraded", "upgrades", "raised", "raises",
    "boost", "boosted", "boosts", "launch", "launched",
    "breakthrough", "innovation", "innovative", "partnership", "award",
    "awarded", "approval", "approved", "approve", "buyback",
    "repurchase", "reward", "rewarding",
}

# Words that flip the polarity of the sentiment word that follows them.
# "not strong" and "failed to beat" are both positive-word sentences with
# negative meaning, and a bare word count reads them exactly backwards.
#
# "declined", "less" and "fewer" were removed: they are weak negators that
# flipped far more than they fixed ("declined less than expected", "fewer
# concerns"), and each is also a sentiment word, so a hit counted twice.
NEGATORS = {
    "not", "no", "never", "none", "cannot", "cant", "can't", "wont", "won't",
    "didnt", "didn't", "doesnt", "doesn't", "isnt", "isn't", "arent",
    "aren't", "wasnt", "wasn't", "werent", "weren't", "failed", "fails",
    "fail", "without", "lack", "lacks", "lacking", "unable",
    "avoid", "avoids", "avoided", "prevent", "prevents",
}
NEGATION_WINDOW = 3   # how many following tokens a negator reaches

# ---------------------------------------------------------------------------
# Phrases, which outrank the word count.
#
# Financial headlines run on idioms whose meaning is not the sum of their
# words, and a bare count reads several of them exactly backwards:
#
#     "failed to beat estimates"   contains a POSITIVE word
#     "cuts costs"                 contains a NEGATIVE one
#     "raises full-year outlook"   the direction is in the verb, not the noun
#
# Each entry is (pattern, weight). Weight is in "sentiment word" units, so a
# phrase counting 2.0 outweighs a single incidental word — which is the point:
# an idiom is unambiguous where its parts are not.
#
# MATCHES DO NOT OVERLAP, and the order below is the priority. "Fails to beat
# consensus" contains "beat consensus": scored independently the two cancel to
# nothing, when what the sentence plainly says is that the company missed. So
# a span of text is claimed by the first pattern that matches it and is then
# closed to the rest — which also stops "cuts its dividend" being counted once
# as a guidance cut and again as a dividend cut.
#
# Negations and misses are therefore listed BEFORE the positive forms they
# contain. That ordering is load-bearing, not cosmetic.
#
# Fillers are `[\w-]+` rather than `\w+` because compound modifiers are
# everywhere in this vocabulary — "cuts full-year delivery forecast" does not
# match a pattern whose gap cannot cross a hyphen, and that one silently cost
# every guidance cut written in the ordinary way.
# ---------------------------------------------------------------------------
_PHRASE_SOURCE: list[tuple[str, float]] = [
    # ---- negations first: they contain the positive forms ----
    (r"\b(?:fail\w*|unable|struggl\w+)\s+to\s+(?:[\w-]+\s+){0,2}"
     r"(?:beat|top|meet|match|exceed|deliver)\b", -2.5),
    # ---- results against expectations ----
    (r"\b(?:miss\w*|falls?\s+short\s+of|shy\s+of|come[s]?\s+in\s+below)\s+"
     r"(?:[\w-]+\s+){0,2}(?:estimates?|expectations?|forecasts?|consensus|"
     r"views?)\b", -2.5),
    (r"\b(?:beats?|beat|tops?|topped|exceeds?|surpass\w*)\s+"
     r"(?:[\w-]+\s+){0,2}(?:estimates?|expectations?|forecasts?|consensus|"
     r"views?)\b", 2.5),
    (r"\brecord\s+(?:loss|losses|low|lows|outflows?|decline|write)", -2.0),
    (r"\brecord\s+(?:high|quarter|revenue|sales|profits?|earnings|backlog|"
     r"orders?|bookings?)\b", 2.0),

    # ---- guidance. The dividend forms come first: "cuts its dividend" also
    # matches the generic guidance-cut pattern, and only one should count. ----
    (r"\b(?:cut[s]?|suspend\w*|elimina\w+|omit\w*|slash\w*)\s+"
     r"(?:its\s+)?dividend\b", -3.0),
    (r"\b(?:raise[sd]?|lift[sed]*|boost\w*|hike[sd]?|increase[sd]?)\s+"
     r"(?:its\s+)?dividend\b", 2.5),
    (r"\b(?:profit|earnings|revenue)\s+warning\b", -3.0),
    (r"\b(?:withdraw\w*|suspend\w*|pull\w*)\s+(?:[\w-]+\s+){0,2}"
     r"(?:guidance|outlook|forecast)\b", -3.0),
    (r"\b(?:cut[s]?|lower[sed]*|slash\w*|reduce[sd]?|trim[s|med]*|"
     r"downgrade[sd]?)\s+(?:[\w-]+\s+){0,3}(?:guidance|outlook|forecast|"
     r"target|estimates?|view)\b", -2.5),
    (r"\b(?:raise[sd]?|lift[sed]*|boost\w*|hike[sd]?|increase[sd]?)\s+"
     r"(?:[\w-]+\s+){0,3}(?:guidance|outlook|forecast|target|estimates?|"
     r"view)\b", 2.5),

    # ---- costs and efficiency: a "cut" that is good news ----
    (r"\b(?:cut[s]?|reduce[sd]?|lower[sed]*|trim[s|med]*)\s+"
     r"(?:[\w-]+\s+){0,2}(?:costs?|expenses?|debt|leverage|spending)\b", 1.5),

    # ---- analyst actions ----
    (r"\b(?:upgrade[sd]?)\s+to\b|\bupgraded\s+(?:by|at)\b", 2.0),
    (r"\b(?:downgrade[sd]?)\s+to\b|\bdowngraded\s+(?:by|at)\b", -2.0),
    (r"\bprice\s+target\s+(?:raise[sd]?|lifted|increase[sd]?|hiked?)\b", 2.0),
    (r"\bprice\s+target\s+(?:cut|lowered|reduce[sd]?|slashed|trimmed)\b", -2.0),
    (r"\b(?:initiate[sd]?\s+(?:\w+\s+){0,2}coverage\s+with\s+"
     r"(?:a\s+)?(?:buy|outperform|overweight))\b", 1.5),

    # ---- what the shares did ----
    #
    # The filler excludes `outstanding` and `sold`. "Shares outstanding" is
    # structural vocabulary, not a price move, and letting it through made
    # "Shares outstanding rise after issuance" read as a rally.
    (r"\b(?:shares?|stock)\s+(?!outstanding|sold)(?:[\w-]+\s+){0,2}"
     r"(?:jump\w*|surge[sd]?|soar\w*|rall\w+|climb\w*|rise[s]?|rose|"
     r"gain\w*|spike[sd]?|pop\w*)\b", 2.0),
    (r"\b(?:shares?|stock)\s+(?!outstanding|sold)(?:[\w-]+\s+){0,2}"
     r"(?:fall\w*|fell|drop\w*|slide[s]?|slid|sink\w*|sank|plunge[sd]?|"
     r"tumble[sd]?|slump\w*|crater\w*)\b", -2.0),
    (r"\bunder\s+pressure\b", -1.5),
    (r"\b52-week\s+high\b", 1.5),
    (r"\b52-week\s+low\b", -1.5),

    # ---- capital returns and structure ----
    (r"\b(?:announce[sd]?|approve[sd]?|expand\w*|authoriz\w*)\s+"
     r"(?:[\w-]+\s+){0,3}(?:buyback|share\s+repurchase|repurchase\s+program)\b", 2.0),
    (r"\bspecial\s+dividend\b", 2.0),
    (r"\bstock\s+split\b", 1.0),
    (r"\bsecondary\s+offering\b|\bdilutive\s+offering\b", -1.5),

    # ---- legal, regulatory, accounting ----
    (r"\b(?:sec|doj|ftc)\s+(?:investigat\w+|prob\w+|charg\w+|sues?)\b", -3.0),
    (r"\bclass[- ]action\b", -2.0),
    (r"\brestat\w+\s+(?:its\s+)?(?:financials?|results?|earnings)\b", -3.0),
    (r"\bmaterial\s+weakness\b", -3.0),
    (r"\b(?:fda|regulator\w*)\s+(?:reject\w+|declin\w+|rebuff\w+)\b", -3.0),
    (r"\b(?:fda|regulator\w*)\s+(?:approv\w+|clear\w+)\b", 2.5),
    (r"\bcomplete\s+response\s+letter\b", -2.5),
    (r"\b(?:loses?|lost)\s+(?:[\w-]+\s+){0,2}(?:case|appeal|ruling|contract|"
     r"bid)\b", -2.0),
    (r"\b(?:wins?|won)\s+(?:[\w-]+\s+){0,2}(?:case|appeal|ruling|contract|"
     r"approval|bid|order)\b", 2.0),

    # ---- corporate events ----
    (r"\b(?:to\s+be\s+)?acquired\s+by\b|\bagrees?\s+to\s+be\s+(?:acquired|"
     r"bought)\b|\btakeover\s+(?:bid|offer)\b", 2.0),
    (r"\b(?:step\w*\s+down|resign\w*|ousted|fired)\b", -1.5),
    (r"\bjob\s+cuts?\b|\blay(?:s|ing)?\s+off\b", -1.5),
    (r"\bgoing\s+concern\b|\bchapter\s+11\b|\bfiles?\s+for\s+bankruptcy\b", -3.0),
]

PHRASES: list[tuple[re.Pattern, float]] = [
    (re.compile(pattern, re.I), weight) for pattern, weight in _PHRASE_SOURCE
]

# How quickly confidence approaches 1 as evidence accumulates. With k = 2.0,
# one sentiment word gives 0.33 confidence, two 0.50, four 0.67, eight 0.80 —
# so a headline containing a single incidental word can no longer return the
# same maximum-confidence verdict as one where six words agree.
CONFIDENCE_HALF_WEIGHT = 2.0

# Below this, a score is reported as "neutral" rather than as a weak direction.
# A deadband, not a rounding: +0.04 and -0.04 are the same reading, and giving
# them opposite labels invents a distinction the method cannot support.
NEUTRAL_BAND = 0.15

_TOKEN = re.compile(r"[a-z][a-z'\-]*")

_loaded: tuple[set[str], set[str]] | None = None


def load_dictionary() -> tuple[set[str], set[str]]:
    """(positive, negative) word sets.

    Uses the full Loughran-McDonald master CSV when `FINLAKE_LM_DICTIONARY`
    points at one, falling back to the bundled subset. The CSV format is the
    published one: a `Word` column plus `Positive`/`Negative` columns holding
    the year a word was added, or 0 when it belongs to neither list.
    """
    global _loaded
    if _loaded is not None:
        return _loaded

    path = os.environ.get("FINLAKE_LM_DICTIONARY")
    if path and Path(path).exists():
        try:
            pos, neg = set(), set()
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    word = (row.get("Word") or "").strip().lower()
                    if not word:
                        continue
                    if (row.get("Positive") or "0") not in ("0", ""):
                        pos.add(word)
                    if (row.get("Negative") or "0") not in ("0", ""):
                        neg.add(word)
            if pos and neg:
                _loaded = (pos, neg)
                return _loaded
        except (OSError, csv.Error, KeyError):
            pass   # malformed file: fall back rather than fail the build

    _loaded = (POSITIVE, NEGATIVE)
    return _loaded


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def label_for(tone: float | None) -> str | None:
    """Positive, negative, or neutral — the reading a person actually wants.

    None stays None. "No sentiment vocabulary was found" is not a neutral
    verdict, it is the absence of one, and a reader who sees "neutral" beside
    a headline will reasonably believe the text was read and judged balanced.
    """
    if tone is None:
        return None
    if tone > NEUTRAL_BAND:
        return "positive"
    if tone < -NEUTRAL_BAND:
        return "negative"
    return "neutral"


def score(text: str) -> dict:
    """Tone of a piece of financial text.

    Returns:

      `tone`        [-1, 1], polarity scaled by how much evidence produced it.
      `label`       'positive' | 'neutral' | 'negative', or None when unscored.
      `polarity`    the raw Loughran-McDonald (pos - neg) / (pos + neg).
      `confidence`  [0, 1), how much sentiment evidence the text carried.
      `positive` / `negative` / `words` / `hits`  — the trace behind the score.

    **`tone` is None when no sentiment words or phrases appear at all.** That
    is the whole point of returning a dict rather than a float: "no sentiment
    vocabulary found" and "equally positive and negative" both compute to zero
    and mean completely different things. One is an absence of evidence; the
    other is genuinely neutral coverage. Collapsing them lets an article
    nobody wrote anything meaningful about count as a real observation.

    **`tone` is polarity times confidence, and that is a change.** Polarity
    alone is the LM standard, and it is right for a 10-K — hundreds of
    sentiment words, and the ratio between them is meaningful. A headline
    carries one or two, so polarity alone returned +1.00 or -1.00 for 85% of
    everything in the cache: a single incidental word delivering a
    maximum-confidence verdict. Scaling by evidence keeps the direction and
    stops one word from shouting. `polarity` is returned unchanged beside it
    for anyone who wants the literature's number.
    """
    positive, negative = load_dictionary()
    text = text or ""
    tokens = tokenize(text)

    pos = neg = 0.0
    hits: list[str] = []

    # ---- phrases first --------------------------------------------------
    # An idiom is unambiguous where its parts are not, so it carries more
    # weight than an incidental word and is credited even though the words
    # inside it are counted again below. "Beats estimates" reading as +2.5
    # from the phrase and +1 from "beats" is the right emphasis, not double
    # counting to be corrected.
    #
    # Spans are claimed exclusively, in PHRASES order. Without that,
    # "fails to beat consensus" matched both the failure and the beat and
    # they cancelled to zero, and "cuts its dividend" was charged twice.
    claimed: list[tuple[int, int]] = []
    for pattern, weight in PHRASES:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            if weight > 0:
                pos += weight
            else:
                neg += -weight
            hits.append(f"[{match.group(0).strip().lower()}]")

    # ---- individual words -----------------------------------------------
    for i, token in enumerate(tokens):
        polarity = 1 if token in positive else (-1 if token in negative else 0)
        if polarity == 0:
            continue
        # Look back a short window for a negator, and flip if one is there.
        window = tokens[max(0, i - NEGATION_WINDOW):i]
        if any(w in NEGATORS for w in window):
            polarity = -polarity
            hits.append(f"NOT {token}")
        else:
            hits.append(token)
        if polarity > 0:
            pos += 1
        else:
            neg += 1

    total = pos + neg
    if total <= 0:
        return {"tone": None, "label": None, "polarity": None,
                "confidence": 0.0, "positive": 0, "negative": 0,
                "words": len(tokens), "hits": hits}

    polarity_score = (pos - neg) / total
    confidence = total / (total + CONFIDENCE_HALF_WEIGHT)
    tone = polarity_score * confidence
    return {
        "tone": tone,
        "label": label_for(tone),
        "polarity": polarity_score,
        "confidence": confidence,
        # Rounded to whole units for storage: these columns exist to let a
        # reader reconstruct the count, and a phrase's 2.5 is not a count.
        "positive": int(round(pos)),
        "negative": int(round(neg)),
        "words": len(tokens),
        "hits": hits,
    }
