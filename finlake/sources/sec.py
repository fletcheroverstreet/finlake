"""SEC EDGAR ingestion.

Three endpoints do almost everything:

  /files/company_tickers_exchange.json  -> ticker <-> CIK <-> exchange
  /api/xbrl/companyfacts/CIK##########.json -> every XBRL fact ever filed
  /submissions/CIK##########.json       -> the filing index (dates, forms)

companyfacts is the good one. Each fact already carries `accn`, `filed`, and
`form`, which means the SEC hands you point-in-time data for free. Most people
throw those fields away during ingestion and then wonder why their backtest
beats the market by 40%/yr.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any, Iterator

from .. import config, store
from ..http_client import _limiter, sec_get

FACT_COLUMNS = (
    "cik", "taxonomy", "tag", "unit", "period_start", "period_end",
    "val", "fy", "fp", "form", "accn", "filed", "frame",
)


def cik_str(cik: int) -> str:
    return f"CIK{int(cik):010d}"


# ---------------------------------------------------------------------------
# Ticker universe
# ---------------------------------------------------------------------------
def load_ticker_map(conn: sqlite3.Connection, *, today: str | None = None) -> int:
    """Refresh ticker <-> CIK.

    The SEC file is a *current* snapshot with no history, so we build history
    ourselves: rows we've never seen open with valid_from = today; rows that
    disappear get closed with valid_to = today. Run this on a schedule and you
    accumulate a real mapping history. Run it once and you have today's only —
    which is fine to start, as long as you know that's what you have.
    """
    today = today or dt.date.today().isoformat()
    payload = sec_get(
        f"{config.SEC_WWW}/files/company_tickers_exchange.json",
        max_age_days=config.STALE_AFTER_DAYS["company_tickers"],
    )
    fields = payload["fields"]          # ['cik','name','ticker','exchange']
    idx = {name: i for i, name in enumerate(fields)}

    seen: set[tuple[str, int]] = set()
    new_rows = []
    for row in payload["data"]:
        cik = int(row[idx["cik"]])
        ticker = (row[idx["ticker"]] or "").upper().strip()
        if not ticker:
            continue
        exch = row[idx["exchange"]]
        name = row[idx["name"]]
        seen.add((ticker, cik))

        cur = conn.execute(
            "SELECT 1 FROM ticker_map WHERE ticker=? AND cik=? AND valid_to IS NULL",
            (ticker, cik),
        ).fetchone()
        if cur is None:
            new_rows.append((ticker, cik, exch, today, None))

        conn.execute(
            """INSERT INTO securities (cik, name, updated_at) VALUES (?,?,?)
               ON CONFLICT(cik) DO UPDATE SET name=excluded.name,
                                              updated_at=excluded.updated_at""",
            (cik, name, today),
        )

    store.upsert_many(
        conn, "ticker_map",
        ("ticker", "cik", "exchange", "valid_from", "valid_to"),
        new_rows,
    )

    # Close out mappings that vanished from the file (delisting / ticker
    # change) -- but ONLY the ones this file is the source of.
    #
    # A mapping resolved through EDGAR's company search is not in the ticker
    # file and never will be, so "missing from the file" says nothing about
    # it. Closing those too meant AEP was resolved by name, written to
    # ticker_map, and un-mapped again by the very next build: facts and
    # filings present under CIK 4904, ticker resolving to nothing, and the
    # company absent from every screen with no error to say why.
    live = conn.execute(
        "SELECT ticker, cik FROM ticker_map "
        "WHERE valid_to IS NULL AND COALESCE(source, 'sec') = 'sec'"
    ).fetchall()
    for r in live:
        if (r["ticker"], r["cik"]) not in seen:
            conn.execute(
                "UPDATE ticker_map SET valid_to=? "
                "WHERE ticker=? AND cik=? AND valid_to IS NULL",
                (today, r["ticker"], r["cik"]),
            )
    conn.commit()
    return len(new_rows)


def resolve_cik_by_name(company: str, *, ticker: str | None = None) -> int | None:
    """Find a CIK from a company name, via EDGAR's company search.

    A fallback for names the SEC's own ticker files miss. They do miss them:
    both `company_tickers.json` and `company_tickers_exchange.json` carry
    10,398 entries and NEITHER contains AEP (American Electric Power), an
    S&P 500 utility. The ticker files are a convenience index, not the
    authoritative company registry, and they lag reorganizations and
    re-registrations.

    Also the entry point for repairing a broken CIK chain: when a holdco
    reorganization mints a new CIK and orphans the predecessor's filings, the
    predecessor is still findable by name.

    Returns the CIK of the best match, or None. Deliberately conservative —
    it returns a result only when the search yields an unambiguous company
    match, because a wrong CIK silently attributes one company's financials
    to another, which is far worse than a missing one.
    """
    query = (company or "").strip()
    if not query:
        return None

    # Fetched directly rather than through sec_get: this endpoint answers in
    # Atom XML, and sec_get parses every response as JSON. It still goes
    # through the shared token bucket, so it counts against the same SEC rate
    # limit as everything else.
    import requests

    try:
        _limiter("www.sec.gov", config.SEC_RATE_LIMIT).acquire()
        resp = requests.get(
            f"{config.SEC_WWW}/cgi-bin/browse-edgar",
            params={"company": query, "action": "getcompany", "type": "10-K",
                    "dateb": "", "owner": "exclude", "count": "10",
                    "output": "atom"},
            headers={"User-Agent": config.SEC_USER_AGENT}, timeout=30,
        )
        if resp.status_code != 200:
            return None
        text = resp.text
    except Exception:
        return None

    ciks = re.findall(r"CIK=(\d{10})", text)
    if not ciks:
        return None

    unique = {int(c) for c in ciks}
    if len(unique) == 1:
        return unique.pop()
    # Several matches: refuse rather than guess. A wrong CIK is worse than
    # no CIK — it attributes another company's financials to this ticker.
    return None


def has_live_mapping(conn: sqlite3.Connection, ticker: str) -> bool:
    """Whether this ticker currently resolves to a company.

    Distinct from `resolve_cik(conn, ticker)`, which deliberately falls back
    to the most recent CLOSED mapping when no live one exists — the right
    answer for "what did this ticker mean", and the wrong one for "is this
    ticker being maintained". Conflating them is why the AEP repair looked
    like it had worked: `resolve_cik` returned 4904 from a closed row, so the
    code that would have re-opened the mapping never ran.
    """
    return conn.execute(
        "SELECT 1 FROM ticker_map WHERE ticker=? AND valid_to IS NULL",
        (ticker.upper().strip(),),
    ).fetchone() is not None


def record_ticker_mapping(conn: sqlite3.Connection, ticker: str, cik: int, *,
                          exchange: str | None = None,
                          source: str = "declared",
                          today: str | None = None) -> bool:
    """Record a ticker -> CIK mapping the SEC's own ticker files do not carry.

    WITHOUT THIS, RESOLVING A CIK BY NAME ACHIEVES NOTHING. `resolve_cik_by_name`
    exists because `company_tickers.json` and `company_tickers_exchange.json`
    both miss real companies — neither of their 10,398 entries is AEP, an
    S&P 500 utility — and the builder uses it to find the CIK so facts and
    filings can be pulled. But every read path (`fundamentals`, the refresh
    universe, `pit.universe`) goes ticker -> CIK through `ticker_map`, and
    nothing was writing the mapping back. So the facts landed, the filings
    landed, and the ticker still resolved to nothing: AEP had zero rows in
    `securities`, `ticker_map`, `facts` and `filings`, and was simply absent
    from every screen with no error anywhere to say so.

    Returns True if a new mapping was opened. Idempotent, and it never touches
    a ticker that already has a live mapping — a mapping observed in the SEC's
    file is better evidence than one inferred from a constituent list, and
    overwriting it is how a ticker gets silently attached to the wrong filer.

    `source` is anything other than 'sec', and that is what stops the next
    `load_ticker_map` closing the mapping again for being absent from a file
    it was never in.

    REPLACE, not IGNORE. `valid_from` is part of the primary key, so a row
    closed out earlier TODAY collides with the one being written — and under
    the store's default INSERT OR IGNORE the write silently did nothing,
    leaving the mapping closed and the repair looking like it had worked.
    """
    ticker = ticker.upper().strip()
    today = today or dt.date.today().isoformat()
    if has_live_mapping(conn, ticker):
        return False

    store.upsert_many(
        conn, "ticker_map",
        ("ticker", "cik", "exchange", "valid_from", "valid_to", "source"),
        [(ticker, int(cik), exchange, today, None, source)],
        ignore_conflicts=False,
    )
    conn.commit()
    return True


def backfill_ticker_validity(conn: sqlite3.Connection) -> int:
    """Extend each ticker's validity window back to the company's first filing.

    THE PROBLEM. The SEC's ticker file is a current snapshot with no history,
    so `load_ticker_map` opens every mapping it has never seen with
    `valid_from = today`. That is honest, but it means a mapping's history
    starts the first time you happen to run the builder — and every as-of
    query before that date resolves to no ticker at all. `pit.universe()`
    drops rows with a null ticker, so *any* historical run returns an EMPTY
    universe, not a partial one. Every ticker in this cache was opened on the
    day the builder first ran, which is why historical runs currently produce
    nothing.

    THE FIX AND ITS ASSUMPTION. Today's ticker -> CIK mapping is assumed to
    have held back to that company's first filing. That assumption is wrong
    only where a ticker was reused or reassigned, so it is applied ONLY to
    tickers with exactly one mapping and no closed (`valid_to IS NOT NULL`)
    history — i.e. where we have no evidence of reuse and nothing to
    contradict. A ticker we have actually watched change hands keeps its real
    windows untouched.

    This is strictly better than the status quo (no history at all) and
    strictly worse than a purchased ticker-history dataset. It is recorded on
    each row via `valid_from_inferred = 1` so nothing downstream mistakes an
    inferred window for an observed one.

    Idempotent: re-running never widens a window that is already correct.
    """
    _ensure_ticker_map_columns(conn)

    # Tickers with exactly one row and no closed history: safe to extend.
    # A ticker that has ever been reassigned is excluded by the HAVING clause.
    candidates = conn.execute(
        """
        SELECT tm.ticker, tm.cik, tm.valid_from, s.first_filed
        FROM ticker_map tm
        JOIN securities s ON s.cik = tm.cik
        WHERE tm.valid_to IS NULL
          AND s.first_filed IS NOT NULL
          AND s.first_filed < tm.valid_from
          AND tm.ticker IN (
              SELECT ticker FROM ticker_map
              GROUP BY ticker
              HAVING COUNT(*) = 1 AND SUM(valid_to IS NOT NULL) = 0
          )
        """
    ).fetchall()

    n = 0
    for r in candidates:
        # valid_from is part of the primary key, so this is a key rewrite.
        # The candidate query guarantees one row per ticker, so no conflict
        # is possible, but OR IGNORE keeps a surprise from aborting the batch.
        cur = conn.execute(
            """UPDATE OR IGNORE ticker_map
               SET valid_from = ?, valid_from_inferred = 1
               WHERE ticker = ? AND cik = ? AND valid_from = ?""",
            (r["first_filed"], r["ticker"], r["cik"], r["valid_from"]),
        )
        n += cur.rowcount
    conn.commit()
    return n


def _ensure_ticker_map_columns(conn: sqlite3.Connection) -> None:
    """Add `valid_from_inferred` to an existing ticker_map, once.

    A migration rather than a schema change alone, because this runs against
    caches built before the column existed (the one on this machine has 10,398
    rows) and rebuilding those from scratch would mean re-downloading
    everything for no reason.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ticker_map)")}
    if "valid_from_inferred" not in cols:
        conn.execute(
            "ALTER TABLE ticker_map ADD COLUMN valid_from_inferred INTEGER DEFAULT 0"
        )
        conn.commit()


def resolve_cik(conn: sqlite3.Connection, ticker: str,
                as_of: str | None = None) -> int | None:
    """Ticker -> CIK, honouring the validity window if as_of is given."""
    ticker = ticker.upper().strip()
    if as_of:
        row = conn.execute(
            """SELECT cik FROM ticker_map
               WHERE ticker=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)
               ORDER BY valid_from DESC LIMIT 1""",
            (ticker, as_of, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT cik FROM ticker_map WHERE ticker=?
               ORDER BY (valid_to IS NULL) DESC, valid_from DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    return int(row["cik"]) if row else None


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def _iter_facts(cik: int, payload: dict[str, Any]) -> Iterator[tuple]:
    """Flatten the nested companyfacts JSON into DB rows.

    Shape: facts -> taxonomy -> tag -> units -> unit -> [ {fact}, ... ]
    """
    for taxonomy, tags in (payload.get("facts") or {}).items():
        for tag, tag_body in tags.items():
            for unit, entries in (tag_body.get("units") or {}).items():
                for e in entries:
                    val, end, accn, filed = (
                        e.get("val"), e.get("end"), e.get("accn"), e.get("filed")
                    )
                    # accn+filed are mandatory: without them the row is useless
                    # for PIT and would silently corrupt as-of queries.
                    if val is None or not end or not accn or not filed:
                        continue
                    yield (
                        cik, taxonomy, tag, unit,
                        e.get("start"), end, float(val),
                        e.get("fy"), e.get("fp"), e.get("form"),
                        accn, filed, e.get("frame"),
                    )


def load_company_facts(conn: sqlite3.Connection, cik: int) -> int:
    payload = sec_get(
        f"/api/xbrl/companyfacts/{cik_str(cik)}.json",
        max_age_days=config.STALE_AFTER_DAYS["companyfacts"],
        allow_404=True,
    )
    if not payload:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
            (f"companyfacts:{cik}", dt.datetime.now().isoformat(), "missing",
             "no XBRL facts (foreign filer, or pre-2009)"),
        )
        return 0

    rows = list(_iter_facts(cik, payload))
    n = store.upsert_many(conn, "facts", FACT_COLUMNS, rows)
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
        (f"companyfacts:{cik}", dt.datetime.now().isoformat(), "ok", f"{n} facts"),
    )
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Submissions / filing index
# ---------------------------------------------------------------------------
def _submission_rows(cik: int, block: dict[str, Any]) -> list[tuple]:
    """One filing-index block (`filings.recent`, or an overflow file) -> rows.

    The block is column-oriented: parallel arrays keyed by field name, not a
    list of records. zip() over the five arrays we care about rebuilds the
    records. `primaryDocument` is occasionally absent from older overflow
    files, so it is padded rather than assumed present — zip() would
    otherwise silently truncate the whole block to zero rows.
    """
    accns = block.get("accessionNumber", [])
    if not accns:
        return []
    n = len(accns)

    def col(name: str) -> list:
        vals = block.get(name, [])
        return list(vals) + [None] * (n - len(vals))

    return [
        (accn, cik, form, filed, period or None, doc)
        for accn, form, filed, period, doc in zip(
            accns, col("form"), col("filingDate"), col("reportDate"),
            col("primaryDocument"),
        )
    ]


def load_submissions(conn: sqlite3.Connection, cik: int) -> int:
    """Filing index + the listing window we use to reconstruct the universe.

    Reads the FULL filing history, not just `filings.recent`.

    This matters more than it looks. The SEC caps `filings.recent` at roughly
    the last 1,000 filings; everything older is paged into separate JSON files
    listed under `filings.files`. A long-lived filer (AAPL, JPM, GE) blows
    through 1,000 filings in well under a decade, so reading only `recent`
    gives you a truncated index AND — because `first_filed` is derived from
    the filings table — a `first_filed` that can be fifteen years too late.

    That second effect is the damaging one: `pit.universe()` gates on
    `first_filed <= as_of`, so a truncated index silently removes the oldest,
    largest companies from every historical screen. The symptom looks like a
    universe-construction problem and is actually an ingestion problem.
    """
    payload = sec_get(
        f"/submissions/{cik_str(cik)}.json",
        max_age_days=config.STALE_AFTER_DAYS["submissions"],
        allow_404=True,
    )
    if not payload:
        return 0

    filings = payload.get("filings", {}) or {}
    rows = _submission_rows(cik, filings.get("recent", {}) or {})

    # Overflow files hold everything older than `recent`. Each entry is
    # {"name": "CIK##########-submissions-001.json", ...} served from the same
    # /submissions/ path. A failure here degrades to a shorter history rather
    # than losing the filings we did get, so it is caught per file.
    for extra in filings.get("files", []) or []:
        name = (extra or {}).get("name")
        if not name:
            continue
        try:
            older = sec_get(
                f"/submissions/{name}",
                max_age_days=config.STALE_AFTER_DAYS["submissions"],
                allow_404=True,
            )
        except Exception:
            continue
        if older:
            rows.extend(_submission_rows(cik, older))

    store.upsert_many(
        conn, "filings",
        ("accn", "cik", "form", "filed", "period", "primary_doc"),
        rows,
    )

    bounds = conn.execute(
        "SELECT MIN(filed) lo, MAX(filed) hi FROM filings WHERE cik=?", (cik,)
    ).fetchone()
    sic = payload.get("sic")
    conn.execute(
        """INSERT INTO securities (cik, name, sic, sic_desc, fiscal_year_end,
                                   first_filed, last_filed, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(cik) DO UPDATE SET
             name=COALESCE(excluded.name, securities.name),
             sic=COALESCE(excluded.sic, securities.sic),
             sic_desc=COALESCE(excluded.sic_desc, securities.sic_desc),
             fiscal_year_end=COALESCE(excluded.fiscal_year_end,
                                      securities.fiscal_year_end),
             first_filed=MIN(COALESCE(excluded.first_filed, '9999'),
                             COALESCE(securities.first_filed, '9999')),
             last_filed=MAX(COALESCE(excluded.last_filed, ''),
                            COALESCE(securities.last_filed, '')),
             updated_at=excluded.updated_at""",
        (cik, payload.get("name"), sic, payload.get("sicDescription"),
         payload.get("fiscalYearEnd"), bounds["lo"], bounds["hi"],
         dt.datetime.now().date().isoformat()),
    )
    conn.commit()
    return len(rows)
