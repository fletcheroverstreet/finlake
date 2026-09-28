"""Company news, from several sources at once.

Each source is a small adapter behind one interface, so a feed that dies,
changes shape, or starts rate-limiting costs you that feed and nothing else.
No source is load-bearing.

Three things this does that a naive scraper doesn't:

**Deduplicates across sources.** A Reuters story reaches Yahoo, Google News,
and two aggregators within minutes. Counted once each, "widely syndicated"
becomes "five independent pieces of news", which is exactly the quantity the
news signal is trying to measure. Articles are keyed on a normalized title,
so the same story from five feeds is one row with five sources noted.

**Weights sources by what they are.** An 8-K is the company telling the SEC
something material, under penalty of law. A press release is the company
telling its own story. An aggregator headline is neither. These are not equal
evidence and are not weighted equally.

**Classifies the event.** "Company announces buyback" and "company announces
restatement" are both news; only the event class makes the difference usable
for anything but a vibe.

Everything here is RSS and Atom — published feeds, meant to be read by
machines. No paywall circumvention, no article-body scraping, and SEC
requests carry the contact User-Agent the SEC requires.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import quote_plus

from .. import config, lexicon, store
from ..http_client import _limiter, sec_get

NEWS_RATE_LIMIT = 1.0     # per host. Feeds are small; there is no hurry.

# How much each source's word is worth, used to weight the aggregate signal.
# An SEC filing is the company under legal obligation; a wire release is the
# company's own framing; an aggregator is a headline with no editor attached.
SOURCE_WEIGHTS = {
    "sec": 1.00,
    "yahoo": 0.85,
    "yfinance": 0.85,
    "google_news": 0.65,
    "pr_wire": 0.60,
}
DEFAULT_WEIGHT = 0.50

# Publishers whose byline raises the weight of whatever feed carried them.
# A Reuters story is a Reuters story regardless of which aggregator it
# arrived through.
TRUSTED_PUBLISHERS = {
    "reuters": 0.95, "bloomberg": 0.95, "associated press": 0.95, "ap": 0.95,
    "wall street journal": 0.92, "wsj": 0.92, "financial times": 0.92,
    "ft": 0.92, "cnbc": 0.85, "barron's": 0.85, "barrons": 0.85,
    "marketwatch": 0.80, "forbes": 0.70, "business wire": 0.60,
    "pr newswire": 0.60, "globenewswire": 0.60, "seeking alpha": 0.55,
    "motley fool": 0.45, "zacks": 0.45, "insider monkey": 0.35,
    "simply wall st": 0.35,
}

# Event classes, checked in order — the first pattern to match wins, so the
# more specific and higher-consequence classes are listed first. A story about
# an earnings-driven downgrade is an earnings story, not an analyst story.
EVENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("earnings", re.compile(
        r"\b(earnings|eps|quarterly results|q[1-4] results|reports? (?:third|fourth|first|second) quarter"
        r"|beats?|misses?|topped estimates|profit (?:rose|fell|jumped))\b", re.I)),
    ("guidance", re.compile(
        r"\b(guidance|outlook|forecast|raises? (?:its )?(?:full[- ]year|fy)|cuts? (?:its )?(?:full[- ]year|fy)"
        r"|expects?|projects?)\b", re.I)),
    ("ma", re.compile(
        r"\b(acquisi\w+|acquires?|acquired|merger|merges?|takeover|buyout"
        r"|to buy|stake in|divest\w*|spin[- ]?off|sells? (?:its )?(?:unit|division|business))\b", re.I)),
    ("legal", re.compile(
        r"\b(lawsuit|sues?|sued|litigation|investigat\w+|probe|subpoena|antitrust"
        r"|settlement|settles?|fined?|penalt\w+|sec charges|doj|regulator\w*"
        r"|recall|violation)\b", re.I)),
    ("management", re.compile(
        r"\b(ceo|cfo|coo|chief execut\w+|chief financial|chairman|president)\b.{0,40}"
        r"\b(step\w* down|resign\w*|depart\w*|appoint\w*|names?|hires?|replac\w*|out)\b"
        r"|\b(names?|appoints?) new\b", re.I)),
    ("capital_return", re.compile(
        r"\b(dividend|buyback|repurchase|share repurchase|special dividend"
        r"|returns? capital)\b", re.I)),
    ("analyst", re.compile(
        r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiate[sd]? coverage"
        r"|reiterate[sd]?|raises? target|cuts? target|overweight|underweight"
        r"|outperform|underperform)\b", re.I)),
    ("product", re.compile(
        r"\b(launch\w*|unveil\w*|introduc\w*|release[sd]?|announce[sd]? (?:new|the)"
        r"|partnership|deal with|contract|approval|fda)\b", re.I)),
]

# Strip the syndication noise that stops two copies of one story matching:
# " - Reuters", " | CNBC", trailing tickers in parentheses, punctuation.
_TITLE_NOISE = re.compile(
    r"\s*[\-\|–—]\s*(reuters|bloomberg|cnbc|marketwatch|barron'?s|yahoo finance|"
    r"the motley fool|zacks|seeking alpha|investing\.com|benzinga|forbes|"
    r"business wire|pr newswire|globenewswire)\s*$", re.I)
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


@dataclass
class Article:
    """One story, before it is scored and stored."""

    title: str
    url: str | None
    source: str
    published_at: str
    summary: str | None = None
    publisher: str | None = None
    tickers: set[str] = field(default_factory=set)
    # Overrides title-based deduplication where a title is not unique.
    # Needed for SEC filings: EVERY 8-K is titled "8-K - Current report", so
    # a title key collapses a company's entire filing history into one row —
    # 40 filings became 1. News titles ARE the right key (that is how one
    # syndicated story is recognised across five feeds); filings need their
    # accession number instead.
    dedupe_key: str | None = None
    # The feed's own identifier for this item. Informational by default; a
    # source whose titles are not unique promotes it into `dedupe_key`.
    feed_id: str | None = None

    @property
    def id(self) -> str:
        return article_id(self.dedupe_key or self.title)


def article_id(title: str) -> str:
    """Stable dedupe key: a hash of the normalized title.

    Title rather than URL, deliberately. The same story carries a different
    URL on every syndicating site, so a URL key deduplicates nothing at all —
    which is the failure that turns one widely-carried story into five
    independent-looking events.
    """
    text = _TITLE_NOISE.sub("", (title or "").lower())
    text = _NON_WORD.sub(" ", text)
    return hashlib.sha256(_WS.sub(" ", text).strip().encode()).hexdigest()[:20]


def classify(text: str) -> str | None:
    for name, pattern in EVENT_PATTERNS:
        if pattern.search(text or ""):
            return name
    return None


# ---------------------------------------------------------------------------
# 8-K items: structured metadata, not prose.
#
# THIS IS WHAT MADE NEWS READ NEGATIVE. An 8-K's summary is a list of item
# codes and their official titles, written once by the SEC and identical
# across every filer. Scoring that with a headline sentiment lexicon is a
# category error, and it went the way category errors go: Item 5.02 is titled
# "Departure of Directors or Certain Officers; Election of Directors;
# Appointment of Certain Officers", "departure" was in the negative list, and
# Item 5.02 is the single most common 8-K there is — routine board elections
# and compensation arrangements.
#
# Result: ~19,600 filings scored a flat -1.00, at source weight 1.00, the
# highest in the system. The SEC feed averaged -0.99 while every other feed
# averaged around +0.5. Nothing failed, nothing logged, and the news bucket
# quietly leaned negative for the entire universe.
#
# So item codes are read as codes. Most are procedural and carry NO tone at
# all — which is different from neutral, and is stored as NULL exactly the way
# an unscored headline is. Only the items whose direction is unambiguous
# regardless of context get a prior, and those priors are deliberately few.
# ---------------------------------------------------------------------------
ITEM_SENTIMENT: dict[str, float] = {
    "1.03": -0.90,   # Bankruptcy or receivership
    "4.02": -0.90,   # Non-reliance on previously issued financial statements
    "3.01": -0.80,   # Notice of delisting / failure to satisfy a listing rule
    "2.04": -0.60,   # Triggering events accelerating a financial obligation
    "2.06": -0.50,   # Material impairments
    "2.05": -0.30,   # Costs associated with exit or disposal activities
    "4.01": -0.20,   # Change of certifying accountant
    "1.02": -0.20,   # Termination of a material definitive agreement
    "3.02": -0.15,   # Unregistered sales of equity securities (dilution)
}

# Item -> event class. Item 2.02 is the earnings release itself, which is the
# highest-information filing a company makes and carries no direction of its
# own: the numbers inside it decide, and the wires report those separately.
ITEM_EVENT: dict[str, str] = {
    "2.02": "earnings",
    "2.01": "ma",
    "1.03": "legal", "3.01": "legal", "4.01": "legal", "4.02": "legal",
    "2.04": "legal", "2.06": "legal",
    "5.01": "management", "5.02": "management",
    "7.01": "guidance",
    "8.01": None,     # "Other Events" — genuinely unclassifiable
}

# Short readable names, for the headline. The SEC's own titles are accurate
# and unreadable — Item 5.02's runs to 130 characters — so these are
# shortened, not reworded.
ITEM_TITLES: dict[str, str] = {
    "1.01": "Material agreement signed",
    "1.02": "Material agreement terminated",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Acquisition or disposition completed",
    "2.02": "Results of operations",
    "2.03": "New financial obligation",
    "2.04": "Obligation accelerated",
    "2.05": "Exit or disposal costs",
    "2.06": "Material impairment",
    "3.01": "Delisting notice",
    "3.02": "Unregistered equity sale",
    "3.03": "Security holder rights modified",
    "4.01": "Auditor change",
    "4.02": "Prior financials not reliable",
    "5.01": "Change in control",
    "5.02": "Board or officer change",
    "5.03": "Bylaws or fiscal year amended",
    "5.07": "Shareholder vote",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}

_ITEM_CODE = re.compile(r"\bItem\s+(\d{1,2}\.\d{2})\b", re.I)


def filing_items(text: str) -> list[str]:
    """The 8-K item codes cited in a filing's summary, in order."""
    seen, out = set(), []
    for code in _ITEM_CODE.findall(text or ""):
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def score_filing(text: str) -> tuple[float | None, str | None, list[str]]:
    """(tone, event_class, items) for an SEC filing, from its item codes.

    Returns tone None for the procedural majority. An 8-K that says the board
    approved a compensation arrangement is not neutral news and it is not bad
    news — it is not news with a direction at all, and the only honest score
    for it is no score.
    """
    items = filing_items(text)
    if not items:
        return None, None, []

    priors = [ITEM_SENTIMENT[i] for i in items if i in ITEM_SENTIMENT]
    # The most severe item drives the tone. A filing pairing a bankruptcy
    # notice with a routine exhibit is a bankruptcy notice.
    tone = min(priors) if priors else None

    event = None
    for item in items:
        mapped = ITEM_EVENT.get(item)
        if mapped:
            event = mapped
            break
    return tone, event, items


def source_weight(source: str, publisher: str | None) -> float:
    """Reliability weight, taking the better of the feed and the byline."""
    base = SOURCE_WEIGHTS.get(source, DEFAULT_WEIGHT)
    if publisher:
        key = publisher.strip().lower()
        for name, weight in TRUSTED_PUBLISHERS.items():
            if name in key:
                return max(base, weight)
    return base


# ---------------------------------------------------------------------------
# Feed parsing. RSS and Atom in one pass — the tag names differ, the shape
# does not, and taking a dependency for this would be more code than this is.
# ---------------------------------------------------------------------------
def _text(node, *paths: str) -> str | None:
    for path in paths:
        found = node.find(path)
        if found is not None:
            value = (found.text or "").strip()
            if not value and path.endswith("link"):
                value = (found.get("href") or "").strip()
            if value:
                return value
    return None


def _parse_date(raw: str | None) -> str | None:
    """Feed date -> ISO date. Feeds use at least four formats between them."""
    if not raw:
        return None
    raw = raw.strip()
    formats = (
        "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    # Last resort: an ISO-looking prefix.
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return None


def _strip_namespaces(root: ET.Element) -> ET.Element:
    """Drop XML namespace prefixes from every tag in the tree.

    Atom puts everything in a default namespace, so an element that reads as
    `<title>` in the raw feed is actually `{http://www.w3.org/2005/Atom}title`
    to ElementTree, and a plain `find("title")` silently matches nothing. That
    is a quiet failure, not an error: the SEC's 8-K feed parsed fine, yielded
    40 entries, and produced ZERO articles because every title lookup missed.

    Stripping prefixes once here means RSS and Atom are the same shape
    downstream and every lookup is a plain tag name.
    """
    for node in root.iter():
        if isinstance(node.tag, str) and "}" in node.tag:
            node.tag = node.tag.rsplit("}", 1)[1]
    return root


def parse_feed(xml_text: str, source: str) -> list[Article]:
    """RSS 2.0 or Atom -> articles. Never raises on malformed XML."""
    try:
        root = _strip_namespaces(ET.fromstring(xml_text))
    except ET.ParseError:
        return []

    items = root.findall(".//item") or root.findall(".//entry")

    out = []
    for item in items:
        title = _text(item, "title")
        if not title:
            continue
        published = _parse_date(
            _text(item, "pubDate", "published", "updated", "filing-date"))
        if not published:
            continue
        summary = _text(item, "description", "summary")
        if summary:
            summary = _WS.sub(" ", re.sub(r"<[^>]+>", " ", summary)).strip()[:1000]
        out.append(Article(
            title=_WS.sub(" ", title).strip(),
            url=_text(item, "link"),
            source=source,
            published_at=published,
            summary=summary,
            publisher=_text(item, "source", "credit"),
            # Captured, but NOT used as the dedupe key by default. Every feed
            # mints its own guid for the same story, so keying on it would
            # defeat cross-source deduplication entirely — the one thing this
            # module most needs to get right. Only sources with non-unique
            # titles (SEC filings) promote it, and they do so explicitly.
            feed_id=_text(item, "guid", "id"),
        ))
    return out


def _fetch_feed(url: str, source: str, *, host: str) -> list[Article]:
    """GET a feed and parse it. A dead feed returns nothing, never raises."""
    import requests

    try:
        _limiter(host, NEWS_RATE_LIMIT).acquire()
        resp = requests.get(
            url, timeout=20,
            headers={"User-Agent": config.SEC_USER_AGENT,
                     "Accept": "application/rss+xml, application/xml, text/xml"})
        if resp.status_code != 200:
            return []
        return parse_feed(resp.text, source)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# The sources
# ---------------------------------------------------------------------------
def from_yahoo_rss(ticker: str) -> list[Article]:
    arts = _fetch_feed(
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(ticker)}"
        f"&region=US&lang=en-US",
        "yahoo", host="feeds.finance.yahoo.com")
    for a in arts:
        a.tickers.add(ticker)
    return arts


def from_google_news(ticker: str, company: str | None = None) -> list[Article]:
    # Quote the company name so a multi-word name isn't parsed as separate
    # terms, and add "stock" to keep a ticker like "F" or "ALL" from matching
    # the entire English language.
    query = f'"{company}" stock' if company else f"{ticker} stock"
    arts = _fetch_feed(
        f"https://news.google.com/rss/search?q={quote_plus(query)}"
        f"&hl=en-US&gl=US&ceid=US:en",
        "google_news", host="news.google.com")
    for a in arts:
        a.tickers.add(ticker)
        # Google appends " - Publisher" to every headline; that suffix is the
        # only byline attribution the feed carries.
        if not a.publisher and a.title:
            match = re.search(r"\s-\s([^-]{2,40})$", a.title)
            if match:
                a.publisher = match.group(1).strip()
    return arts


def from_sec_filings(ticker: str, cik: int) -> list[Article]:
    """Material-event filings as news.

    The highest-signal "news" there is: an 8-K is a company telling the SEC
    something material, on a deadline, under penalty of law — not a
    journalist's read of it.

    Two adjustments the other sources don't need. Filings dedupe on their
    accession number rather than their title, because every 8-K carries the
    same title. And the title is rewritten to include the date, since "8-K -
    Current report" repeated forty times down a news feed tells a reader
    nothing.
    """
    url = (f"{config.SEC_WWW}/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={cik:010d}&type=8-K&dateb=&owner=include&count=40"
           f"&output=atom")
    arts = _fetch_feed(url, "sec", host="www.sec.gov")
    for a in arts:
        a.tickers.add(ticker)
        a.publisher = "SEC EDGAR"
        # The accession number lives in the entry <id>, shaped like
        # "urn:tag:sec.gov,2008:accession-number=0000320193-26-000075", and
        # falls back to the document URL, which also contains it.
        a.dedupe_key = a.feed_id or a.url or f"{ticker}-8k-{a.published_at}"
        form = a.title.split(" - ")[0].strip() or "8-K"
        # Name the items in the headline. "8-K — AAPL material event" repeated
        # down a feed tells a reader nothing; "8-K · Results of operations"
        # tells them whether to open it.
        items = filing_items(a.summary or "")
        what = ", ".join(ITEM_TITLES.get(i, f"Item {i}") for i in items[:2])
        a.title = (f"{form} · {what}" if what
                   else f"{form} filed {a.published_at} — {ticker}")
    return arts


def from_yfinance(ticker: str) -> list[Article]:
    """The market provider's own curated feed, which carries real bylines."""
    try:
        import yfinance as yf

        _limiter("query2.finance.yahoo.com", NEWS_RATE_LIMIT).acquire()
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out = []
    for item in items:
        # The provider moved these fields under a "content" key at some point
        # and still returns the flat shape for some symbols. Handle both.
        body = item.get("content") if isinstance(item.get("content"), dict) else item
        title = body.get("title") or item.get("title")
        if not title:
            continue

        published = body.get("pubDate") or body.get("displayTime")
        if not published and item.get("providerPublishTime"):
            try:
                published = dt.datetime.fromtimestamp(
                    int(item["providerPublishTime"])).date().isoformat()
            except (TypeError, ValueError, OSError):
                published = None
        published = _parse_date(published) or dt.date.today().isoformat()

        provider = body.get("provider")
        publisher = (provider.get("displayName") if isinstance(provider, dict)
                     else item.get("publisher"))
        link = body.get("canonicalUrl") or {}
        out.append(Article(
            title=str(title).strip(),
            url=(link.get("url") if isinstance(link, dict) else None) or item.get("link"),
            source="yfinance",
            published_at=published,
            summary=(body.get("summary") or body.get("description") or None),
            publisher=publisher,
            tickers={ticker},
        ))
    return out


SOURCES: dict[str, Callable] = {
    "yahoo": from_yahoo_rss,
    "google_news": from_google_news,
    "yfinance": from_yfinance,
    "sec": from_sec_filings,
}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def _store(conn: sqlite3.Connection, articles: Iterable[Article]) -> int:
    """Score, dedupe, and write. Returns the number of NEW articles."""
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    merged: dict[str, Article] = {}
    for art in articles:
        if not art.title or not art.published_at:
            continue
        existing = merged.get(art.id)
        if existing is None:
            merged[art.id] = art
        else:
            # Same story from a second feed: keep the higher-weighted source
            # and union the tickers rather than storing it twice.
            existing.tickers |= art.tickers
            if (source_weight(art.source, art.publisher)
                    > source_weight(existing.source, existing.publisher)):
                art.tickers = existing.tickers
                merged[art.id] = art

    rows, links = [], []
    for art in merged.values():
        text = f"{art.title}. {art.summary or ''}"
        if art.source == "sec":
            # A filing is item codes, not prose. Scoring it as prose is what
            # made routine 8-Ks the most negative "news" in the database —
            # see the note above ITEM_SENTIMENT.
            tone_value, event, _items = score_filing(text)
            positive = negative = 0
            words = len(lexicon.tokenize(text))
            # Fall back to the text classifier only when no item code
            # resolved, which happens when the feed omits the summary.
            event = event or classify(art.title)
        else:
            scored = lexicon.score(text)
            tone_value = scored["tone"]
            positive, negative = scored["positive"], scored["negative"]
            words = scored["words"]
            event = classify(text)

        rows.append((
            art.id, art.url, art.source, art.publisher, art.title, art.summary,
            art.published_at, fetched_at, event, tone_value,
            positive, negative, words,
            source_weight(art.source, art.publisher),
        ))
        links.extend((art.id, t.upper()) for t in art.tickers)

    before = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    store.upsert_many(
        conn, "news_articles",
        ("id", "url", "source", "publisher", "title", "summary",
         "published_at", "fetched_at", "event_class", "sentiment",
         "pos_count", "neg_count", "word_count", "source_weight"),
        rows,
    )
    store.upsert_many(conn, "news_tickers", ("id", "ticker"), links)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] - before


def load_news(conn: sqlite3.Connection, ticker: str, *, cik: int | None = None,
              company: str | None = None,
              sources: list[str] | None = None) -> dict[str, int]:
    """Fetch news for one ticker from every source, dedupe, score, store.

    Returns per-source article counts. Each source is isolated: one dead feed
    costs that feed's articles and nothing else.
    """
    ticker = ticker.upper()
    wanted = sources or list(SOURCES)
    collected: list[Article] = []
    counts: dict[str, int] = {}

    for name in wanted:
        try:
            if name == "sec":
                if cik is None:
                    continue
                got = from_sec_filings(ticker, cik)
            elif name == "google_news":
                got = from_google_news(ticker, company)
            else:
                got = SOURCES[name](ticker)
        except Exception:
            got = []
        counts[name] = len(got)
        collected.extend(got)

    counts["new"] = _store(conn, collected)
    counts["total_seen"] = len(collected)
    return counts


# Event classes carry different weight in the aggregate signal. An 8-K or an
# earnings report moves a business; an analyst note is a re-read of facts
# already public; an unclassified headline is mostly noise. These are
# multipliers on the source weight, not replacements for it.
EVENT_WEIGHTS = {
    "earnings": 1.00,
    "guidance": 1.00,     # forward-looking, so arguably the most informative
    "legal": 0.90,
    "ma": 0.85,
    "management": 0.75,
    "capital_return": 0.70,
    "product": 0.50,
    "analyst": 0.40,
    None: 0.30,
}


def signal(conn: sqlite3.Connection, tickers: list[str], as_of: str, *,
           lookback_days: int = 60, half_life_days: float = 20.0) -> dict[str, dict]:
    """Aggregate news signal per ticker: event-weighted, time-decayed tone.

    Three weights multiply together on each article:

      *source*  — an 8-K outranks a wire release outranks an aggregator.
      *event*   — an earnings report outranks an analyst note outranks noise.
      *recency* — exponential decay at `half_life_days`, so a story from two
                  months ago counts a fraction of one from this morning.

    Returns, per ticker: `score` in [-1, 1], the article and scored-article
    counts, and `coverage`.

    **Coverage is reported honestly and separately from score.** A ticker with
    no articles gets `score=None, coverage=0.0` — never a fabricated neutral
    zero. Those are different claims: one says the news was balanced, the
    other says we know nothing. Feeding a fabricated zero into a z-score
    places a name we have no information about squarely at the sector average,
    which reads as a real finding.

    `volume_ratio` compares article count in the window against the ticker's
    own trailing baseline. A company suddenly generating three times its usual
    coverage is informative regardless of tone.
    """
    if not tickers:
        return {}

    as_of_date = dt.date.fromisoformat(as_of)
    since = (as_of_date - dt.timedelta(days=lookback_days)).isoformat()
    # Baseline window: the four lookback periods before this one.
    base_since = (as_of_date - dt.timedelta(days=lookback_days * 5)).isoformat()

    placeholders = ",".join("?" * len(tickers))
    upper = [t.upper() for t in tickers]

    rows = conn.execute(
        f"""SELECT t.ticker, a.published_at, a.sentiment, a.event_class,
                   a.source_weight, a.pos_count, a.neg_count
            FROM news_articles a
            JOIN news_tickers t ON t.id = a.id
            WHERE t.ticker IN ({placeholders})
              AND a.published_at <= ? AND a.published_at >= ?""",
        (*upper, as_of, base_since),
    ).fetchall()

    out = {t: {"ticker": t, "score": None, "articles": 0, "scored": 0,
               "coverage": 0.0, "volume_ratio": None, "events": {},
               "positive": 0, "neutral": 0, "negative": 0, "unscored": 0}
           for t in upper}

    weighted: dict[str, list[tuple[float, float]]] = {t: [] for t in upper}
    baseline: dict[str, int] = {t: 0 for t in upper}

    for r in rows:
        ticker = r["ticker"]
        if r["published_at"] < since:
            baseline[ticker] += 1        # older window: volume baseline only
            continue

        entry = out[ticker]
        entry["articles"] += 1
        cls = r["event_class"]
        entry["events"][cls or "other"] = entry["events"].get(cls or "other", 0) + 1

        if r["sentiment"] is None:
            entry["unscored"] += 1       # no sentiment vocabulary: not scored
            continue

        label = lexicon.label_for(float(r["sentiment"]))
        if label:
            entry[label] += 1

        age = (as_of_date - dt.date.fromisoformat(r["published_at"])).days
        decay = 0.5 ** (max(age, 0) / half_life_days)
        # A FOURTH weight: how much sentiment evidence the article carried.
        # Without it a headline containing one incidental word counts exactly
        # as much as one where six words agree — which, with the old
        # saturating tone, is how a handful of stray matches could set the
        # whole signal for a quiet name.
        hits = float((r["pos_count"] or 0) + (r["neg_count"] or 0))
        confidence = (hits / (hits + lexicon.CONFIDENCE_HALF_WEIGHT)
                      if hits > 0 else 0.34)
        weight = (r["source_weight"] or DEFAULT_WEIGHT) \
            * EVENT_WEIGHTS.get(cls, EVENT_WEIGHTS[None]) * decay * confidence
        weighted[ticker].append((float(r["sentiment"]), weight))
        entry["scored"] += 1

    for ticker, pairs in weighted.items():
        entry = out[ticker]
        total_weight = sum(w for _s, w in pairs)
        if total_weight > 0:
            entry["score"] = sum(s * w for s, w in pairs) / total_weight
            # Coverage grows with how much scored evidence there is and
            # saturates. The threshold is 5 rather than 10 because every
            # weight now carries a confidence factor below 1, which shrank
            # the same evidence to roughly half its former total.
            entry["coverage"] = min(1.0, total_weight / 5.0)

        prior = baseline[ticker] / 4.0   # baseline spans 4 lookback windows
        if prior >= 1.0:
            entry["volume_ratio"] = entry["articles"] / prior

    return out


def rescore(conn: sqlite3.Connection, *, batch: int = 2000) -> dict[str, int]:
    """Re-score and re-classify every stored article in place.

    Needed because the scoring changed, not the data. Articles are stored once
    with their tone alongside, so a cache built under the old lexicon keeps
    serving the old verdicts forever — including the ~19,600 routine 8-K
    filings sitting at -1.00 apiece — until something walks the table.

    Rewrites `title` for filings too, since the old title ("8-K filed
    2026-04-20 — AAPL material event") carried none of the information the
    item codes in the summary already held. The row `id` is not touched: a
    filing's id hashes its accession number, not its title, so the identity
    that dedupe depends on is unaffected.

    Idempotent. Run it as often as you like.
    """
    rows = conn.execute(
        "SELECT id, source, title, summary, published_at FROM news_articles"
    ).fetchall()

    counts = {"total": len(rows), "filings": 0, "articles": 0,
              "now_unscored": 0, "sign_flipped": 0}
    before = {
        r["id"]: r["sentiment"]
        for r in conn.execute("SELECT id, sentiment FROM news_articles")
    }

    updates = []
    for r in rows:
        title, summary = r["title"], r["summary"]
        text = f"{title}. {summary or ''}"
        if r["source"] == "sec":
            tone, event, items = score_filing(text)
            positive = negative = 0
            words = len(lexicon.tokenize(text))
            event = event or classify(title)
            what = ", ".join(ITEM_TITLES.get(i, f"Item {i}") for i in items[:2])
            form = (title.split("·")[0].split("filed")[0].strip()
                    or "8-K")
            if what:
                title = f"{form} · {what}"
            counts["filings"] += 1
        else:
            scored = lexicon.score(text)
            tone = scored["tone"]
            positive, negative = scored["positive"], scored["negative"]
            words = scored["words"]
            event = classify(text)
            counts["articles"] += 1

        was = before.get(r["id"])
        if was is not None and tone is None:
            counts["now_unscored"] += 1
        elif was is not None and tone is not None and was * tone < 0:
            counts["sign_flipped"] += 1

        updates.append((title, event, tone, positive, negative, words, r["id"]))

    for i in range(0, len(updates), batch):
        conn.executemany(
            "UPDATE news_articles SET title=?, event_class=?, sentiment=?, "
            "pos_count=?, neg_count=?, word_count=? WHERE id=?",
            updates[i:i + batch])
        conn.commit()
    return counts


def recent(conn: sqlite3.Connection, ticker: str, *, days: int = 60,
           as_of: str | None = None, limit: int = 200) -> list[dict]:
    """Articles for a ticker in the window ending at `as_of`.

    The `as_of` bound is what keeps news point-in-time like everything else:
    an article published tomorrow must be invisible to a query dated today.
    """
    as_of = as_of or dt.date.today().isoformat()
    since = (dt.date.fromisoformat(as_of) - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT a.* FROM news_articles a
           JOIN news_tickers t ON t.id = a.id
           WHERE t.ticker = ? AND a.published_at <= ? AND a.published_at >= ?
           ORDER BY a.published_at DESC LIMIT ?""",
        (ticker.upper(), as_of, since, limit),
    ).fetchall()
    return [dict(r) for r in rows]
