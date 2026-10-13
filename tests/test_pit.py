"""Point-in-time tests.

These run offline against synthetic facts. They encode the two failure modes
your spec names — restatements and lookahead — as assertions, so a future
refactor can't quietly reintroduce them.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_")

from finlake import pit, store  # noqa: E402

FACT_COLS = ("cik", "taxonomy", "tag", "unit", "period_start", "period_end",
             "val", "fy", "fp", "form", "accn", "filed", "frame")


def fact(cik, tag, end, val, accn, filed, start=None, form="10-Q",
         fy=None, fp=None):
    return (cik, "us-gaap", tag, "USD", start, end, val, fy, fp, form,
            accn, filed, None)


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    conn.executescript("DELETE FROM facts; DELETE FROM securities;"
                       "DELETE FROM ticker_map; DELETE FROM macro;")
    conn.commit()
    return conn


def test_restatement_is_invisible_before_it_was_filed():
    """The core promise. FY2018 revenue was 1000 as originally filed, restated
    to 900 in 2021. A screen run in March 2019 must see 1000."""
    conn = fresh_db()
    store.upsert_many(conn, "facts", FACT_COLS, [
        fact(1, "Revenues", "2018-12-31", 1000, "0001-18-000001", "2019-02-01",
             start="2018-10-01", form="10-K"),
        fact(1, "Revenues", "2018-12-31", 900, "0001-21-000009", "2021-02-15",
             start="2018-10-01", form="10-K/A"),
    ])
    conn.commit()

    seen_2019 = pit.as_of_facts(conn, 1, ["Revenues"], as_of="2019-03-14")
    assert len(seen_2019) == 1
    assert seen_2019[0]["val"] == 1000, "as-of query leaked a future restatement"

    seen_now = pit.as_of_facts(conn, 1, ["Revenues"], as_of="2024-01-01")
    assert seen_now[0]["val"] == 900, "latest-known query missed the restatement"
    print("  ok  restatement respects the filed bound")


def test_company_that_ipod_later_cannot_appear():
    """The survivorship killer. A 2021 IPO has zero facts filed by 2019."""
    conn = fresh_db()
    store.upsert_many(conn, "facts", FACT_COLS, [
        fact(2, "Revenues", "2018-12-31", 500, "0002-21-000001", "2021-06-30",
             start="2018-10-01", form="10-K"),
    ])
    conn.commit()

    assert pit.as_of_facts(conn, 2, ["Revenues"], as_of="2019-03-14") == []
    assert len(pit.as_of_facts(conn, 2, ["Revenues"], as_of="2022-01-01")) == 1
    print("  ok  pre-IPO backfill is invisible to a 2019 screen")


def test_universe_excludes_unlisted_and_dead():
    conn = fresh_db()
    rows = [
        # (cik, name, first_filed, last_filed)
        (1, "Alive Co",   "2005-01-01", "2026-01-01"),
        (2, "Future Co",  "2021-05-01", "2026-01-01"),   # IPO'd after as_of
        (3, "Dead Co",    "2001-01-01", "2016-06-01"),   # stopped filing
    ]
    for cik, name, first, last in rows:
        conn.execute(
            "INSERT INTO securities (cik,name,first_filed,last_filed) "
            "VALUES (?,?,?,?)", (cik, name, first, last))
        conn.execute(
            "INSERT INTO ticker_map (ticker,cik,exchange,valid_from,valid_to) "
            "VALUES (?,?,?,?,?)",
            (f"T{cik}", cik, "NYSE", "2000-01-01", None))
    conn.commit()

    u = pit.universe(conn, "2019-03-14")
    names = {r["name"] for r in u}
    assert names == {"Alive Co"}, f"universe leaked: {names}"
    print("  ok  universe excludes pre-IPO and stale filers")


def test_macro_vintage():
    """GDP first printed at -4.8, later revised. A signal dated 2020-05-01
    must use the print, not the revision."""
    conn = fresh_db()
    store.upsert_many(conn, "macro",
                      ("series_id", "obs_date", "realtime_start", "value"), [
                          ("GDPC1", "2020-01-01", "2020-04-29", -4.8),
                          ("GDPC1", "2020-01-01", "2020-05-28", -5.0),
                          ("GDPC1", "2020-01-01", "2021-07-29", -5.1),
                      ])
    conn.commit()

    assert pit.macro_as_of(conn, "GDPC1", "2020-05-01")[0]["value"] == -4.8
    assert pit.macro_as_of(conn, "GDPC1", "2022-01-01")[0]["value"] == -5.1
    print("  ok  macro returns the vintage that existed on the date")


def test_restatement_detection():
    conn = fresh_db()
    store.upsert_many(conn, "facts", FACT_COLS, [
        fact(1, "Revenues", "2018-12-31", 1000, "a", "2019-02-01",
             start="2018-10-01"),
        fact(1, "Revenues", "2018-12-31", 900, "b", "2021-02-15",
             start="2018-10-01"),
        fact(1, "Assets", "2018-12-31", 5000, "a", "2019-02-01"),
    ])
    conn.commit()
    assert pit.was_restated(conn, 1, "Revenues", "2018-12-31") is True
    assert pit.was_restated(conn, 1, "Assets", "2018-12-31") is False
    print("  ok  restatement detection")


def test_reloading_a_balance_sheet_fact_does_not_duplicate_it():
    """Regression: an instant fact has a NULL period_start, and SQLite treats
    NULLs in a primary key as distinct, so INSERT OR IGNORE never fired for
    one. Every companyfacts reload appended another copy of the whole balance
    sheet, and restatements() listed each version once per reload."""
    conn = fresh_db()
    assets = fact(1, "Assets", "2018-12-31", 500, "0001-19-000001", "2019-02-01")
    revenue = fact(1, "Revenues", "2018-12-31", 1000, "0001-19-000001",
                   "2019-02-01", start="2018-01-01")
    for _ in range(3):
        store.upsert_many(conn, "facts", FACT_COLS, [assets, revenue])
    conn.commit()

    counts = dict(conn.execute(
        "SELECT tag, COUNT(*) FROM facts GROUP BY tag").fetchall())
    assert counts == {"Assets": 1, "Revenues": 1}
    assert len(pit.restatement_history(conn, 1, "Assets", "2018-12-31")) == 1


def test_init_db_removes_duplicates_from_a_cache_built_before_the_fix():
    """An existing cache already holds the duplicates, and the unique index
    cannot be created over them. init_db sheds the copies -- and only the
    copies: a genuine restatement (a different accn) must survive."""
    conn = fresh_db()
    conn.execute("DROP INDEX IF EXISTS ux_facts_instant")
    original = fact(1, "Assets", "2018-12-31", 500, "0001-19-000001", "2019-02-01")
    restated = fact(1, "Assets", "2018-12-31", 450, "0001-21-000001", "2021-02-01")
    for _ in range(3):
        store.upsert_many(conn, "facts", FACT_COLS, [original, restated])
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 6

    store.init_db(conn)

    history = pit.restatement_history(conn, 1, "Assets", "2018-12-31")
    assert [h["val"] for h in history] == [500, 450]
    # And the hole stays closed for writes after the migration.
    store.upsert_many(conn, "facts", FACT_COLS, [original])
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


if __name__ == "__main__":
    store.init_db()
    for fn in [
        test_restatement_is_invisible_before_it_was_filed,
        test_company_that_ipod_later_cannot_appear,
        test_universe_excludes_unlisted_and_dead,
        test_macro_vintage,
        test_restatement_detection,
        test_reloading_a_balance_sheet_fact_does_not_duplicate_it,
        test_init_db_removes_duplicates_from_a_cache_built_before_the_fix,
    ]:
        fn()
    print("\nall PIT tests passed")
