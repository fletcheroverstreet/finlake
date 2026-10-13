"""Which symbols the refresh tiers keep current.

Offline. Every case here is a regression: each one was live behaviour that
produced no error, no log line, and no visibly wrong number — just a loop
spending its request budget on instruments nothing displays, or a company
quietly absent from every screen.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_universe_")

from finlake import refresh, store  # noqa: E402
from finlake.sources import sec  # noqa: E402


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    refresh.ensure_schema(conn)
    conn.executescript(
        "DELETE FROM universe; DELETE FROM ticker_map; DELETE FROM facts; "
        "DELETE FROM securities;")
    conn.commit()
    return conn


def map_ticker(conn, ticker, cik, *, source="sec", valid_to=None):
    store.upsert_many(
        conn, "ticker_map",
        ("ticker", "cik", "exchange", "valid_from", "valid_to", "source"),
        [(ticker, cik, "NYSE", "2000-01-01", valid_to, source)],
        ignore_conflicts=False)
    conn.commit()


def give_facts(conn, cik):
    store.upsert_many(
        conn, "facts",
        ("cik", "taxonomy", "tag", "unit", "period_start", "period_end",
         "val", "accn", "filed"),
        [(cik, "us-gaap", "Revenues", "USD", "2024-01-01", "2024-03-31",
          1.0, f"accn-{cik}", "2024-04-30")])
    conn.commit()


# ---------------------------------------------------------------------------
# The declared universe
# ---------------------------------------------------------------------------
def test_the_sweep_is_the_declared_universe_not_every_mapped_instrument():
    """THE BUG THIS TABLE EXISTS FOR. `ticker_map` is a list of INSTRUMENTS,
    not companies: the SEC maps a filer's preferred series, warrants and baby
    bonds to the same CIK as its common stock. Sweeping it meant 209 of 711
    symbols were things like ALL-PB, PSA-PJ and OXY-WT — no fundamentals at
    the market provider, five 404s apiece per sweep, and nothing downstream
    that could ever display them."""
    conn = fresh_db()
    give_facts(conn, 899051)
    for symbol in ("ALL", "ALL-PB", "ALL-PH", "ALL-PI", "ALL-PJ"):
        map_ticker(conn, symbol, 899051)

    assert refresh._universe_tickers(conn) == [
        "ALL", "ALL-PB", "ALL-PH", "ALL-PI", "ALL-PJ"], (
        "precondition: without a declared universe every instrument is swept")

    store.declare_universe(conn, [("ALL", 899051, "Allstate")], source="test.csv")
    assert refresh._universe_tickers(conn) == ["ALL"]
    conn.close()


def test_no_declaration_keeps_the_old_behaviour():
    """A cache built before this table existed must not silently start
    refreshing nothing — that would be a worse failure than the one being
    fixed, and an invisible one."""
    conn = fresh_db()
    give_facts(conn, 320193)
    map_ticker(conn, "AAPL", 320193)
    assert refresh._universe_tickers(conn) == ["AAPL"]
    conn.close()


def test_declaring_replaces_rather_than_accumulates():
    """A constituent file is a statement about the whole set. Merging would
    mean the universe could only ever grow, and a company dropped from the
    index would be refreshed forever with nothing displaying it."""
    conn = fresh_db()
    store.declare_universe(
        conn, [("AAPL", 320193, "Apple"), ("XRAY", 818479, "Dentsply")],
        source="old.csv")
    store.declare_universe(conn, [("AAPL", 320193, "Apple")], source="new.csv")

    assert store.universe_members(conn) == ["AAPL"]
    assert store.universe_source(conn) == "new.csv"
    conn.close()


def test_a_declared_universe_needs_no_facts_to_be_swept():
    """Quotes, news and market data do not depend on filings having been
    ingested. Gating the sweep on `facts` is what made a name with a failed
    SEC pull invisible to the refresh loop as well — two failures compounding
    into one silent absence."""
    conn = fresh_db()
    store.declare_universe(conn, [("AEP", 4904, "American Electric Power")],
                           source="test.csv")
    assert refresh._universe_tickers(conn) == ["AEP"]
    conn.close()


def test_limit_applies_to_the_declared_universe():
    conn = fresh_db()
    store.declare_universe(
        conn, [(t, None, None) for t in ("AAPL", "MSFT", "NVDA")],
        source="test.csv")
    assert refresh._universe_tickers(conn, limit=2) == ["AAPL", "MSFT"]
    conn.close()


def test_universe_file_parsing_keeps_the_name_column():
    """The name is not decoration: a ticker the SEC's files do not carry is
    recovered through EDGAR's company search, which matches on NAMES. Handing
    it "AEP" finds nothing; "American Electric Power" resolves immediately."""
    path = os.path.join(tempfile.mkdtemp(), "u.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# provenance: current membership, no add/drop dates\n")
        fh.write("ticker,name,sector,indices,cik\n")
        fh.write("AEP,American Electric Power,Utilities,S&P 500,4904\n")
        fh.write("BRK-B,Berkshire Hathaway,Financials,S&P 500,1067983\n")

    rows = store.read_universe_file(path)
    assert rows == [("AEP", 4904, "American Electric Power"),
                    ("BRK-B", 1067983, "Berkshire Hathaway")], (
        "the header comment block was parsed as data, or the name was dropped")


# ---------------------------------------------------------------------------
# Mappings the SEC's ticker files do not carry
# ---------------------------------------------------------------------------
def test_a_declared_mapping_survives_a_ticker_map_refresh(monkeypatch):
    """AEP, exactly. `load_ticker_map` closes out every live mapping missing
    from the SEC file, on the theory that a disappearance means a delisting.
    A mapping that was never IN that file disappears from it on every run — so
    the repair was written and un-written by the next build, leaving facts and
    filings under a CIK no ticker resolved to."""
    conn = fresh_db()
    map_ticker(conn, "AAPL", 320193)
    assert sec.record_ticker_mapping(conn, "AEP", 4904)

    monkeypatch.setattr(sec, "sec_get", lambda *a, **k: {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
    })
    sec.load_ticker_map(conn, today="2026-08-13")

    assert sec.has_live_mapping(conn, "AEP"), (
        "a mapping the SEC file never carried was closed for being absent "
        "from it")
    assert sec.resolve_cik(conn, "AEP") == 4904
    conn.close()


def test_a_vanished_sec_mapping_is_still_closed(monkeypatch):
    """The other half: this must not become a blanket amnesty. A ticker that
    really was in the SEC file and really has gone is still a delisting."""
    conn = fresh_db()
    map_ticker(conn, "GONE", 111111)

    monkeypatch.setattr(sec, "sec_get", lambda *a, **k: {
        "fields": ["cik", "name", "ticker", "exchange"], "data": [],
    })
    sec.load_ticker_map(conn, today="2026-08-13")

    assert not sec.has_live_mapping(conn, "GONE")
    conn.close()


def test_recording_reopens_a_mapping_closed_earlier_the_same_day():
    """`valid_from` is part of the primary key, so a row closed out TODAY
    collides with the repair being written — and under the store's default
    INSERT OR IGNORE the write silently did nothing. The repair reported
    success and the ticker stayed unmapped."""
    conn = fresh_db()
    today = "2026-08-13"
    store.upsert_many(
        conn, "ticker_map",
        ("ticker", "cik", "exchange", "valid_from", "valid_to", "source"),
        [("AEP", 4904, None, today, today, "sec")], ignore_conflicts=False)
    conn.commit()

    assert sec.record_ticker_mapping(conn, "AEP", 4904, today=today)
    assert sec.has_live_mapping(conn, "AEP")
    conn.close()


def test_recording_never_overwrites_a_live_mapping():
    """A mapping observed in the SEC's own file is better evidence than one
    asserted by a constituent list. Overwriting it is how a ticker gets
    silently attached to the wrong filer, which is worse than any gap."""
    conn = fresh_db()
    map_ticker(conn, "ALL", 899051)
    assert not sec.record_ticker_mapping(conn, "ALL", 999999)
    assert sec.resolve_cik(conn, "ALL") == 899051
    conn.close()


def test_liveness_and_resolvability_are_different_questions():
    """`resolve_cik` deliberately falls back to the most recent CLOSED
    mapping — the right answer for "what did this ticker mean". Using it to
    ask "is this ticker maintained" is why the AEP repair looked like it had
    worked: it answered 4904 from a closed row, so the code that would have
    re-opened the mapping never ran."""
    conn = fresh_db()
    map_ticker(conn, "AEP", 4904, valid_to="2026-08-13")

    assert sec.resolve_cik(conn, "AEP") == 4904
    assert not sec.has_live_mapping(conn, "AEP")
    conn.close()
