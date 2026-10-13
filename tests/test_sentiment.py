"""News sentiment: the scoring, and the things it used to read backwards.

Every case here comes from real cached headlines or real 8-K summaries. The
expected labels are judgements about the TEXT, written down before the
implementation was consulted — the point is to pin what a reader would say,
not to record what the scorer currently outputs.
"""

import os
import tempfile

os.environ.setdefault("FINLAKE_HOME", tempfile.mkdtemp(prefix="finlake_test_sent_"))

import pytest  # noqa: E402

from finlake import lexicon  # noqa: E402
from finlake.sources import news as news_src  # noqa: E402


def label(text: str):
    return lexicon.score(text)["label"]


def tone(text: str):
    return lexicon.score(text)["tone"]


# ---------------------------------------------------------------------------
# The bug that made every ticker's news read negative
# ---------------------------------------------------------------------------
AAPL_5_02 = (
    "Filed: 2026-04-20 AccNo: 0001140361-26-015711 Size: 239 KB "
    "Item 5.02: Departure of Directors or Certain Officers; Election of "
    "Directors; Appointment of Certain Officers: Compensatory Arrangements "
    "of Certain Officers"
)


def test_a_routine_8k_carries_no_tone_at_all():
    """THE bug. Item 5.02 is the most common 8-K there is — board elections
    and compensation arrangements — and its official SEC title contains the
    word "departure". Scored as prose that word was the only sentiment term
    present, so the filing came out at -1.00, the maximum negative reading,
    at source weight 1.00, the highest in the system.

    ~19,600 filings in the cache sat at that value. The SEC feed averaged
    -0.99 while every other feed averaged about +0.5.

    The correct score is not "neutral": a compensation arrangement is not
    balanced news, it is not news with a direction at all, and only NULL says
    that.
    """
    filing_tone, event, items = news_src.score_filing(AAPL_5_02)
    assert filing_tone is None, (
        "a routine board-and-compensation filing is being given a direction")
    assert items == ["5.02"]
    assert event == "management"


def test_the_word_departure_no_longer_drags_a_headline_negative():
    """Belt and braces on the same bug from the lexicon side."""
    assert "departure" not in lexicon.NEGATIVE


def test_genuinely_bad_filings_still_score_negative():
    """The fix must not flatten every filing to nothing. A non-reliance
    notice says last year's financials were wrong, and there is no reading of
    that which is neutral."""
    for summary, ceiling in (
        ("Item 4.02: Non-Reliance on Previously Issued Financial Statements",
         -0.8),
        ("Item 1.03: Bankruptcy or Receivership", -0.8),
        ("Item 3.01: Notice of Delisting or Failure to Satisfy a Continued "
         "Listing Rule", -0.5),
    ):
        filing_tone, _event, _items = news_src.score_filing(summary)
        assert filing_tone is not None and filing_tone <= ceiling, summary


def test_the_most_severe_item_drives_a_multi_item_filing():
    """A filing pairing a bankruptcy notice with a routine exhibit list is a
    bankruptcy notice."""
    filing_tone, _event, items = news_src.score_filing(
        "Item 1.03: Bankruptcy or Receivership "
        "Item 9.01: Financial Statements and Exhibits")
    assert items == ["1.03", "9.01"]
    assert filing_tone == pytest.approx(-0.90)


def test_an_earnings_filing_is_classified_but_not_directed():
    """Item 2.02 is the earnings release itself: the highest-information
    filing a company makes, and one with no direction of its own. The numbers
    inside decide, and the wires report those separately."""
    filing_tone, event, _items = news_src.score_filing(
        "Item 2.02: Results of Operations and Financial Condition")
    assert event == "earnings"
    assert filing_tone is None


# ---------------------------------------------------------------------------
# Saturation: one word must not deliver a maximum-confidence verdict
# ---------------------------------------------------------------------------
def test_a_single_word_is_a_weak_reading_not_a_certain_one():
    """85% of every scored article in the cache sat on exactly +1.00 or
    -1.00, because `(pos - neg) / (pos + neg)` returns ±1 whenever one word
    is found and none disagree — which, in an eight-word headline, is the
    normal case."""
    one_word = lexicon.score("Company announces strong quarter")
    assert one_word["polarity"] == pytest.approx(1.0)      # LM's own number
    assert abs(one_word["tone"]) < 0.4, (
        "a single incidental word is again delivering a maximum verdict")


def test_more_agreeing_evidence_reads_more_strongly():
    weak = abs(tone("Profit rose"))
    strong = abs(tone(
        "Profit rose as margins improved, revenue grew and the company "
        "raised its outlook on strong demand"))
    assert strong > weak


def test_no_sentiment_vocabulary_stays_unscored():
    """"Nothing was found" and "it was balanced" both compute to zero and
    mean opposite things. Only one of them is a reading."""
    scored = lexicon.score(
        "Autodesk, Inc. $ADSK Shares Sold by Bank of America Corp DE")
    assert scored["tone"] is None
    assert scored["label"] is None


def test_the_neutral_band_does_not_swallow_a_real_reading():
    """A deadband that is too wide reports everything as neutral, which is
    just the old problem inverted."""
    assert label("Acme misses estimates, shares tumble 12%") == "negative"
    assert label("Acme beats estimates, shares surge 12%") == "positive"


# ---------------------------------------------------------------------------
# Idioms a word count reads backwards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headline,expected", [
    # "beat" is a positive word; the sentence is not.
    ("XYZ fails to beat consensus, cuts dividend", "negative"),
    # "cut" was a negative word; cutting costs is good news.
    ("Company cuts costs by $2 billion, expands buyback program", "positive"),
    # The direction is in the verb, and the noun is a hyphenated compound —
    # which a gap pattern that cannot cross a hyphen silently misses.
    ("Boeing cuts full-year delivery forecast after 737 delays", "negative"),
    ("Apple raises full-year revenue outlook", "positive"),
    ("Retailer lowers full-year guidance on soft demand", "negative"),
    # "record" is positive in one and negative in the other.
    ("Motorola stock jumps 8% as backlog hits record", "positive"),
    ("Company reports record loss for the quarter", "negative"),
    ("Analyst upgrades Tesla to Buy, price target raised to $500", "positive"),
    ("Broker downgrades the stock to Sell, price target cut", "negative"),
    ("FDA approves Lilly's new obesity drug", "positive"),
    ("SEC investigation into accounting practices sends shares lower",
     "negative"),
])
def test_financial_idioms_read_the_right_way(headline, expected):
    assert label(headline) == expected, lexicon.score(headline)["hits"]


def test_overlapping_phrases_are_counted_once():
    """"Cuts its dividend" matches both the generic guidance-cut pattern and
    the dividend-specific one; "fails to beat consensus" contains "beat
    consensus". Scored independently the second pair cancels to nothing and
    the first is charged twice."""
    hits = lexicon.score("XYZ fails to beat consensus, cuts its dividend")["hits"]
    phrases = [h for h in hits if h.startswith("[")]
    assert len(phrases) == len(set(phrases)), f"duplicate phrase match: {hits}"
    assert not any("beat consensus" in h for h in phrases), (
        "the positive half of 'fails to beat consensus' was credited too")


# ---------------------------------------------------------------------------
# Words that were never opinions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("word", ["outstanding", "record", "high", "leading",
                                  "advance", "advanced"])
def test_structural_vocabulary_is_not_positive_sentiment(word):
    """"Shares outstanding", "on record", "high costs", "leading provider",
    "Advanced Micro Devices" — each was scoring as good news."""
    assert word not in lexicon.POSITIVE


@pytest.mark.parametrize("word", ["issue", "issues", "fine", "cut", "cuts",
                                  "departure", "exit"])
def test_ambiguous_vocabulary_is_not_negative_sentiment(word):
    """"Issues guidance", "a fine quarter", "cuts costs", "exit strategy"."""
    assert word not in lexicon.NEGATIVE


def test_a_negator_is_not_also_counted_as_its_own_sentiment_word():
    """"Declined", "less" and "fewer" were in both lists, so each hit
    counted once as a negative word AND flipped the next one."""
    overlap = lexicon.NEGATORS & (lexicon.NEGATIVE | lexicon.POSITIVE)
    assert overlap <= {"fail", "failed", "fails"}, (
        f"these are both negators and sentiment words: {sorted(overlap)}")


# ---------------------------------------------------------------------------
# The display contract
# ---------------------------------------------------------------------------
def test_label_boundaries_match_the_declared_band():
    band = lexicon.NEUTRAL_BAND
    assert lexicon.label_for(band + 0.01) == "positive"
    assert lexicon.label_for(band) == "neutral"
    assert lexicon.label_for(0.0) == "neutral"
    assert lexicon.label_for(-band) == "neutral"
    assert lexicon.label_for(-band - 0.01) == "negative"
    assert lexicon.label_for(None) is None


def test_the_ui_and_the_scorer_agree_on_where_neutral_ends():
    """The hub draws its own chips from its own constant. If the two drift, a
    headline is labelled neutral on one page and negative on the next."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "lodestar"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from lodestar.ui import theme

    assert theme.TONE_NEUTRAL_BAND == lexicon.NEUTRAL_BAND
