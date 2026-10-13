"""Point-in-time queries.

There is no separate "PIT store". There is one append-only fact table and a
query that filters on `filed`. That's the whole module.

    as_of = None  -> latest known (what you'd use for live screening)
    as_of = date  -> what a reader could have known that morning

Two things the `filed` filter buys you, both from your spec:

  * Restatements. A 2018 revenue number restated in 2021 has two rows. An
    as_of of 2019-03-14 only sees the original — which is what an investor
    standing there actually had.

  * Survivorship / lookahead. A company whose first filing is 2021 has zero
    rows with filed <= 2019-03-14, so it cannot appear in a 2019 screen no
    matter how sloppy the calling code is. The bound does the work.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

# One row per (tag, period_end): the most recent version filed on or before
# as_of. ROW_NUMBER over a partition is the standard bitemporal "as-of" idiom.
_AS_OF_SQL = """
WITH ranked AS (
    SELECT
        tag, unit, period_start, period_end, val, fy, fp, form, accn, filed,
        ROW_NUMBER() OVER (
            PARTITION BY tag, unit, period_end, period_start
            ORDER BY filed DESC, accn DESC
        ) AS rn
    FROM facts
    WHERE cik = :cik
      AND tag IN ({tag_list})
      AND filed <= :as_of
      AND period_end >= :min_period
      {form_clause}
)
SELECT tag, unit, period_start, period_end, val, fy, fp, form, accn, filed
FROM ranked
WHERE rn = 1
ORDER BY tag, period_end
"""

# Amended filings (10-K/A) are legitimate corrections and should be kept.
# 8-K exhibits sometimes carry XBRL that duplicates periodic reports; include
# them only when you explicitly want earnings-release timing.
PERIODIC_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F")


def as_of_facts(
    conn: sqlite3.Connection,
    cik: int,
    tags: list[str],
    *,
    as_of: str | None = None,
    min_period: str = "1990-01-01",
    periodic_only: bool = True,
) -> list[dict]:
    """Resolved facts as known on `as_of`. One row per (tag, period)."""
    as_of = as_of or dt.date.today().isoformat()
    tag_list = ",".join(f":t{i}" for i in range(len(tags)))
    form_clause = (
        "AND form IN (" + ",".join(f"'{f}'" for f in PERIODIC_FORMS) + ")"
        if periodic_only else ""
    )
    sql = _AS_OF_SQL.format(tag_list=tag_list, form_clause=form_clause)

    params: dict[str, object] = {
        "cik": cik, "as_of": as_of, "min_period": min_period
    }
    params.update({f"t{i}": t for i, t in enumerate(tags)})
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def restatement_history(
    conn: sqlite3.Connection, cik: int, tag: str, period_end: str
) -> list[dict]:
    """Every version of one number, oldest first.

    Useful as a sanity check and genuinely interesting on its own: large
    downward restatements are a real signal.
    """
    rows = conn.execute(
        """SELECT val, form, accn, filed, period_start
           FROM facts
           WHERE cik=? AND tag=? AND period_end=?
           ORDER BY filed ASC, accn ASC""",
        (cik, tag, period_end),
    ).fetchall()
    return [dict(r) for r in rows]


def was_restated(
    conn: sqlite3.Connection, cik: int, tag: str, period_end: str,
    tolerance: float = 0.005,
) -> bool:
    """True if any later version differs by more than `tolerance` (fraction)."""
    hist = restatement_history(conn, cik, tag, period_end)
    if len(hist) < 2:
        return False
    first = hist[0]["val"]
    if first == 0:
        return any(h["val"] != 0 for h in hist[1:])
    return any(abs(h["val"] - first) / abs(first) > tolerance for h in hist[1:])


# ---------------------------------------------------------------------------
# Universe reconstruction
# ---------------------------------------------------------------------------
def universe(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    grace_days: int = 200,
    require_recent_filing: bool = True,
    sic_prefix: str | None = None,
) -> list[dict]:
    """Companies plausibly listed and reporting on `as_of`.

    Two filters, both derived from filing behaviour rather than a purchased
    index-membership file:

      first_filed <= as_of        -> hadn't IPO'd yet? can't be in the screen.
      last_filed  >= as_of - grace -> stopped filing? treat as gone.

    `grace_days` exists because a healthy company filing 10-Ks and 10-Qs goes
    at most ~120 days between filings; 200 gives slack for late filers without
    keeping zombies around for years.

    HONEST LIMITATION: this is a filing-activity proxy, not true index
    membership. A company can be listed but not in the S&P 500, and a company
    can deregister without its last filing being obviously final. For a real
    S&P 500 backtest you need a historical constituent file with add/drop
    dates — that is a separate dataset (paid, or scraped from index-committee
    press releases). What this gives you is a defensible *investable universe*,
    which is enough for most quality/value screens and strictly better than
    "today's tickers, applied to 2015".
    """
    cutoff = (
        dt.date.fromisoformat(as_of) - dt.timedelta(days=grace_days)
    ).isoformat()

    sql = """
        SELECT s.cik, s.name, s.sic, s.sic_desc, s.first_filed, s.last_filed,
               (SELECT tm.ticker FROM ticker_map tm
                 WHERE tm.cik = s.cik
                   AND tm.valid_from <= :as_of
                   AND (tm.valid_to IS NULL OR tm.valid_to > :as_of)
                 ORDER BY tm.valid_from DESC LIMIT 1) AS ticker
        FROM securities s
        WHERE s.first_filed IS NOT NULL
          AND s.first_filed <= :as_of
    """
    params: dict[str, object] = {"as_of": as_of, "cutoff": cutoff}
    if require_recent_filing:
        sql += " AND s.last_filed >= :cutoff"
    if sic_prefix:
        sql += " AND s.sic LIKE :sicp"
        params["sicp"] = f"{sic_prefix}%"
    sql += " ORDER BY s.cik"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return [r for r in rows if r["ticker"]]


def macro_as_of(
    conn: sqlite3.Connection, series_id: str, as_of: str | None = None
) -> list[dict]:
    """FRED series as it read on `as_of` — original prints, not revisions.

    GDP, payrolls, and CPI all get revised, sometimes substantially. A macro
    signal backtested on final-revision data is using information that did not
    exist at the trade date.
    """
    as_of = as_of or dt.date.today().isoformat()
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT obs_date, value, realtime_start,
                   ROW_NUMBER() OVER (PARTITION BY obs_date
                                      ORDER BY realtime_start DESC) rn
            FROM macro
            WHERE series_id = ? AND realtime_start <= ?
        )
        SELECT obs_date, value, realtime_start FROM ranked
        WHERE rn = 1 ORDER BY obs_date
        """,
        (series_id, as_of),
    ).fetchall()
    return [dict(r) for r in rows]
