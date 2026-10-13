"""News ingestion: dedupe, sentiment, event classes, and signal aggregation.

Offline and synthetic. Feeds are parsed from fixture XML rather than fetched,
so these test the logic and not the internet.
"""

import datetime as dt
import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_news_")

import pytest  # noqa: E402

from finlake import lexicon, store  # noqa: E402
from finlake.sources import news  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Apple beats Q3 earnings estimates</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 03 Aug 2026 12:00:00 GMT</pubDate>
    <description>Strong results across every segment.</description>
    <guid>yahoo-123</guid>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - Current report</title>
    <link href="https://sec.gov/x/0000320193-26-000075"/>
    <updated>2026-07-30T00:00:00Z</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000075</id>
  </entry>
  <entry>
    <title>8-K - Current report</title>
    <link href="https://sec.gov/x/0000320193-26-000080"/>
    <updated>2026-08-01T00:00:00Z</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000080</id>
  </entry>
</feed>"""


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    conn.executescript("DELETE FROM news_articles; DELETE FROM news_tickers;")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------
def test_rss_and_atom_both_parse():
    """Atom puts every tag in a default namespace, so a plain find("title")
    matches nothing. That failed silently: the SEC 8-K feed parsed fine,
    yielded 40 entries, and produced ZERO articles."""
    assert len(news.parse_feed(RSS, "yahoo")) == 1
    atom = news.parse_feed(ATOM, "sec")
    assert len(atom) == 2, "Atom namespace handling regressed"
    assert atom[0].title.startswith("8-K")
    assert atom[0].published_at == "2026-07-30"


def test_malformed_feed_returns_nothing_rather_than_raising():
    assert news.parse_feed("<not xml", "yahoo") == []
    assert news.parse_feed("", "yahoo") == []


# ---------------------------------------------------------------------------
# Deduplication — the thing this module most needs to get right
# ---------------------------------------------------------------------------
def test_the_same_story_from_five_feeds_is_one_article():
    """A Reuters piece syndicates to Yahoo, Google News and two aggregators
    within minutes. Counted once each, "widely syndicated" becomes "five
    independent pieces of bad news" — which is exactly the quantity the news
    signal is trying to measure."""
    title = "Apple beats Q3 earnings estimates"
    variants = [
        title,
        f"{title} - Reuters",          # Google News appends the publisher
        f"{title} | CNBC",
        title.upper(),
        f"  {title}!  ",
    ]
    ids = {news.article_id(v) for v in variants}
    assert len(ids) == 1, f"syndicated copies produced {len(ids)} distinct ids"


def test_different_stories_are_not_merged():
    a = news.article_id("Apple beats Q3 earnings estimates")
    b = news.article_id("Apple misses Q3 earnings estimates")
    assert a != b


def test_sec_filings_dedupe_on_accession_not_title():
    """EVERY 8-K is titled "8-K - Current report". Keyed on title, a company's
    entire filing history collapses into one row — 40 filings became 1."""
    conn = fresh_db()
    articles = news.parse_feed(ATOM, "sec")
    for a in articles:
        a.dedupe_key = a.feed_id
        a.tickers.add("AAPL")
    news._store(conn, articles)

    n = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    assert n == 2, f"two distinct 8-K filings stored as {n} row(s)"
    conn.close()


def test_feed_id_does_not_leak_into_news_deduplication():
    """Every feed mints its own guid for the same story, so keying news on it
    would defeat cross-source dedupe entirely."""
    parsed = news.parse_feed(RSS, "yahoo")[0]
    assert parsed.feed_id == "yahoo-123"
    assert parsed.dedupe_key is None, (
        "a feed guid was promoted to the dedupe key for a news source, which "
        "stops the same story matching across feeds")


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------
def test_financial_vocabulary_is_not_scored_as_negative():
    """The reason a general-purpose lexicon can't be used. A standard
    sentiment dictionary flags 'liability', 'cost', 'tax' and 'crude' as
    negative; in a financial context they are neutral accounting words, and
    scoring them makes tone track how much accounting a text contains."""
    result = lexicon.score(
        "The company reported tax expense, cost of revenue, depreciation, "
        "liability and crude oil inventory")
    assert result["tone"] is None, (
        f"neutral accounting vocabulary scored {result['tone']} "
        f"(hits: {result['hits']})")


def test_tone_direction_hand_checked():
    good = lexicon.score("Record profit, strong growth, raised guidance")
    bad = lexicon.score("Losses widen amid weak demand and a lawsuit")
    assert good["tone"] > 0.5
    assert bad["tone"] < -0.5


def test_negation_flips_polarity():
    """'not strong' is a positive-word sentence with negative meaning, and a
    bare word count reads it exactly backwards."""
    plain = lexicon.score("Results were strong")
    negated = lexicon.score("Results were not strong")
    assert plain["tone"] > 0
    assert negated["tone"] < 0, "negation was ignored"


def test_no_sentiment_words_is_none_not_zero():
    """'No sentiment vocabulary found' and 'equally positive and negative'
    both compute to zero and mean completely different things. One is an
    absence of evidence; collapsing them lets an article nobody wrote anything
    meaningful about count as a real neutral observation."""
    empty = lexicon.score("Apple scheduled its annual meeting for Tuesday")
    assert empty["tone"] is None

    balanced = lexicon.score("Strong revenue but weak margins")
    assert balanced["tone"] == 0.0, "a genuinely balanced text should score 0"


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headline,expected", [
    ("Apple beats Q3 earnings estimates", "earnings"),
    ("Nvidia raises full-year guidance", "guidance"),
    ("Broadcom to buy VMware in a $61 billion deal", "ma"),
    ("SEC investigates the company over disclosure", "legal"),
    ("Apple CEO Tim Cook steps down", "management"),
    ("Board approves $100 billion buyback", "capital_return"),
    ("Morgan Stanley upgrades Apple to overweight", "analyst"),
    ("Apple unveils new iPhone", "product"),
    ("Apple tests Chinese memory chips", None),
])
def test_event_classification(headline, expected):
    assert news.classify(headline) == expected


def test_source_weighting_prefers_primary_sources():
    """An 8-K is a company telling the SEC something material under penalty
    of law. An aggregator headline is not equivalent evidence."""
    assert news.source_weight("sec", "SEC EDGAR") > \
        news.source_weight("google_news", None)
    # A byline outranks the feed that carried it: a Reuters story is a Reuters
    # story whichever aggregator it arrived through.
    assert news.source_weight("google_news", "Reuters") > \
        news.source_weight("google_news", "Motley Fool")


# ---------------------------------------------------------------------------
# Signal aggregation
# ---------------------------------------------------------------------------
def _article(conn, ticker, aid, published, sentiment, event="earnings",
             weight=0.9):
    store.upsert_many(
        conn, "news_articles",
        ("id", "title", "published_at", "fetched_at", "sentiment",
         "event_class", "source_weight", "source"),
        [(aid, f"headline {aid}", published, published, sentiment, event,
          weight, "test")])
    store.upsert_many(conn, "news_tickers", ("id", "ticker"), [(aid, ticker)])
    conn.commit()


def test_recent_news_outweighs_old_news():
    """Exponential decay at the configured half-life. Two articles of equal
    and opposite tone should not cancel when one is months older."""
    conn = fresh_db()
    today = dt.date(2026, 8, 9)
    _article(conn, "T", "fresh", today.isoformat(), 1.0)
    _article(conn, "T", "stale",
             (today - dt.timedelta(days=60)).isoformat(), -1.0)

    sig = news.signal(conn, ["T"], today.isoformat(),
                      lookback_days=90, half_life_days=20)["T"]
    assert sig["score"] > 0.5, (
        f"a two-month-old article was weighted like today's (score "
        f"{sig['score']:.3f})")
    conn.close()


def test_no_news_reports_zero_coverage_never_a_neutral_score():
    """A fabricated neutral zero places a name we know nothing about squarely
    at the sector average, which reads as a real finding."""
    conn = fresh_db()
    sig = news.signal(conn, ["SILENT"], "2026-08-09")["SILENT"]
    assert sig["score"] is None
    assert sig["coverage"] == 0.0
    assert sig["articles"] == 0
    conn.close()


def test_articles_without_sentiment_words_count_for_volume_not_tone():
    conn = fresh_db()
    _article(conn, "T", "no_words", "2026-08-08", None)
    sig = news.signal(conn, ["T"], "2026-08-09")["T"]
    assert sig["articles"] == 1, "the article should still count as coverage"
    assert sig["scored"] == 0
    assert sig["score"] is None
    conn.close()


def test_future_news_is_invisible_to_an_earlier_query():
    """Point-in-time, applied to news. An article published tomorrow must not
    reach a query dated today."""
    conn = fresh_db()
    _article(conn, "T", "future", "2026-08-20", 1.0)
    _article(conn, "T", "past", "2026-08-01", -1.0)

    sig = news.signal(conn, ["T"], "2026-08-09")["T"]
    assert sig["articles"] == 1, "a future article leaked into the window"
    assert sig["score"] < 0
    conn.close()


def test_higher_weight_events_move_the_score_more():
    """An earnings report and an analyst note are not equal evidence."""
    conn = fresh_db()
    _article(conn, "T", "earn", "2026-08-08", -1.0, event="earnings")
    _article(conn, "T", "note", "2026-08-08", 1.0, event="analyst")
    sig = news.signal(conn, ["T"], "2026-08-09")["T"]
    assert sig["score"] < 0, (
        "an analyst note offset an earnings report one-for-one")
    conn.close()
