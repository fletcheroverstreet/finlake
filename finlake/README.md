# finlake

A point-in-time financial data layer. One module for prices, fundamentals,
filings, market data, news, and macro — cached locally, queryable as of any
historical date.

Built as Tier 0 for a suite of equity-analysis tools, so that every project
above it is a few hundred lines instead of a new codebase. **lodestar**, the
scoring engine and web hub, is built on it and published as its own
repository.

---

## The one idea

**The point-in-time store is not a second database. It is a column.**

`facts` is append-only and bitemporal. Every row carries two dates:

| column       | meaning                                   |
|--------------|-------------------------------------------|
| `period_end` | the period the number describes           |
| `filed`      | the date the number became public         |

A restatement never overwrites anything. It arrives as a new row with a new
accession number and a later `filed`, sitting next to the original. So:

```sql
WHERE filed <= '2019-03-14'   -- what a reader had that morning
WHERE filed <= DATE('now')    -- latest known
```

Same table, same query, one changed bound. If you build the fact table with
grain `(cik, tag, period_end, value)` you will have to tear it out and rebuild
to add PIT later. Build it with `accn` and `filed` from day one and PIT is free.

If you ever write `UPDATE facts SET val = ...`, stop. You are about to destroy
the only thing that makes a backtest honest.

---

## Install

Python 3.12 or newer. Clone this repository, then from inside it:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS / Linux: source .venv/bin/activate
pip install -e .                      # the package and its core dependencies
pip install -r requirements.txt       # optional extras: logo rendering, pytest
copy .env.example .env                # macOS / Linux: cp .env.example .env
```

Then open `.env` and fill in two credentials, both free. It is read
automatically — nothing needs exporting — and it is gitignored, so it never
leaves your machine.

- `SEC_USER_AGENT` — **required**. The SEC returns 403 without a real contact
  address, and IP-bans clients that ignore the rate limit. Use the form
  `finlake/0.13 (you@example.com)`.
- `FRED_API_KEY` — only needed for macro series (rates, inflation, GDP, the
  yield curve). Get one at <https://fredaccount.stlouisfed.org/apikeys>.

The cache lives in `~/.finlake` by default. Set `FINLAKE_HOME` to move it.

---

## Build the cache

```powershell
# The full universe: S&P 500 + Nasdaq-100, with prices, analyst data,
# news and macro. Budget one to two hours and 10 GB or more of disk.
python scripts/build.py --universe-file universe/sp500_ndx.csv --all

# Or start small to see it work in a couple of minutes:
python scripts/build.py --tickers AAPL MSFT NVDA JPM XOM --prices
```

Resumable — every write is `INSERT OR IGNORE` and raw HTTP responses are
cached to disk, so a killed run costs nothing and re-running is cheap.

A full `--universe-file` build also declares that list as the set of symbols
the refresh loop keeps current (see below). To re-declare it without
rebuilding: `python -m finlake universe --file universe/sp500_ndx.csv`.

---

## Keeping it current

```powershell
python -m finlake refresh --daemon     # leave running; Ctrl-C to stop
python -m finlake refresh --status     # what each tier last did, and what is due
python -m finlake universe --list      # which symbols are being kept current
```

Each source refreshes on the cadence it actually changes at: batched quotes
every 3 minutes, news every 5, EDGAR filings every 10, analyst estimates and
targets every 6 hours, FRED daily, logos weekly. A laptop that was closed
overnight catches up on start rather than waiting a full interval.

If you use lodestar, run `python -m lodestar daemon` instead — it drives these
same tiers and also re-scores the universe nightly, so you need only one.

---

## Use

```python
import finlake

# Ten years of clean quarterly fundamentals. Offline. ~16 ms.
df = finlake.fundamentals("AAPL", years=10)

# The same screen, as it looked on that morning.
df = finlake.fundamentals("AAPL", years=10, as_of="2019-03-14")

# Where did each number come from, and when did it go public?
df = finlake.fundamentals("AAPL", years=10, include_provenance=True)
#   -> adds revenue__derived (bool), revenue__filed (date) per concept

# Assembled statements: quarterly, annual, or trailing twelve months.
finlake.income_statement("AAPL", freq="annual")

# Latest valuation at the latest close: P/E, EV/EBITDA, yields, Altman Z ...
finlake.ratios_latest("AAPL", live=True)

# The latest cached price, with the date of the bar it came from.
finlake.quote("AAPL")

# Every published version of one number.
finlake.restatements("GE", "revenue", "2017-12-31")

# Prices. Raw bars cached; adjustment computed at read time.
finlake.prices("AAPL", start="2015-01-01", adjust="total")

# Macro, using the vintage that existed on the date.
finlake.macro("GDPC1", as_of="2020-05-01")   # -> the -4.8% print, not -5.1%

# News for one name, point-in-time like everything else.
finlake.news("AAPL", days=30)

# Investable universe on a historical date.
finlake.universe(as_of="2019-03-14")
```

---

## Architecture

```
finlake/
├── config.py          paths, credentials (.env), rate limits, staleness policy
├── http_client.py     token-bucket limiter + gzipped on-disk response cache
├── store.py           SQLite schema  ← read the design note at the top
├── quarterize.py      XBRL → clean discrete quarters
├── pit.py             as-of queries, restatement history, universe
├── api.py             public surface + concept→tag fallback map
├── statements.py      income statement, balance sheet, cash flow
├── ratios.py          42 ratios: point-in-time history and live valuation
├── quality.py         data-quality checks — identities, coverage, staleness
├── refresh.py         the tiered refresh scheduler
├── lexicon.py         Loughran–McDonald financial sentiment
├── __main__.py        CLI: refresh, universe, health, rescore-news
└── sources/
    ├── sec.py         ticker map, companyfacts, submissions index
    ├── prices.py      raw OHLCV → parquet, actions → SQLite
    ├── market.py      estimates, price targets, short interest, ownership
    ├── news.py        multi-source news, deduplication, sentiment
    ├── fred.py        ALFRED vintages
    └── logos.py       company logos

scripts/
├── build.py                bulk build the cache
├── build_universe.py       regenerate universe/sp500_ndx.csv
├── audit_valuation.py      universe-wide valuation invariants
├── audit_concepts.py       which XBRL tags filers actually use
└── regression_snapshot.py  before/after diff for a data-layer change
```

**SQLite for facts, Parquet for bars.** Facts need indexed range queries on
`filed`, which is SQLite's job. Bars are wide, append-only, and read whole,
which is Parquet's.

**Raw prices, never adjusted.** A split announced next Tuesday changes every
adjusted close back to 1980. Cache `adj_close` and you must rewrite every
ticker that ever splits; miss one and you get a 2× error that looks like alpha.
Cache raw close + a `corp_actions` table and you append one row.

---

## The four traps this handles

**1. Q4 is never filed.** There is no Q4 10-Q — the 10-K reports the full year.
Q4 must be derived as `FY − 9M`. Parsers that expect four 10-Qs silently lose
25% of every income statement.

**2. Most filers tag YTD, not discrete quarters.** A Q3 10-Q may contain a
273-day revenue fact and nothing for the 92-day quarter. You have to difference
the chain: `Q2 = H1 − Q1`, `Q3 = 9M − H1`, `Q4 = FY − 9M`. Filers are
inconsistent about this across years *within one company*, so the strategy has
to be decided per fiscal year, not per company.

**3. Never difference a balance sheet.** Assets at Q3-end minus Assets at
Q2-end is a change in assets, not "Q3 assets". XBRL marks these — instant facts
have no `start`. Detect it from the data; don't hardcode a tag list.

**4. There is no tag called "Revenue".** Apple uses one tag, a 2013 filing uses
another, a bank uses a third. Code against a single tag and you get `NaN` for a
third of the market — and `NaN` gets dropped, and the survivors look unusually
healthy. `api.CONCEPTS` maps each concept to an ordered fallback list.

---

## Checking the data

Wrong data here does not announce itself — it produces a plausible number.
Two tools exist to catch that:

```powershell
python scripts/audit_valuation.py        # three universe-wide invariants
python -m finlake health --ticker AAPL   # per-company data-quality findings
```

`audit_valuation.py` checks that enterprise value equals market cap plus net
debt, and reconciles both market cap and enterprise value against an
independent source. Run it after any change that touches valuation.
`CHANGELOG.md` records the bugs each check has caught.

---

## Tests

```powershell
python -m pytest -q
```

259 tests. All offline, all synthetic, no credentials needed. They encode the
failure modes above as assertions so a refactor can't quietly reintroduce them:

- a 2021 restatement is invisible to an `as_of="2019-03-14"` query
- a company that IPO'd in 2021 cannot appear in a 2019 screen
- Q4 is derived correctly; balance-sheet levels are passed through untouched
- a missing middle quarter produces a gap, not a fabricated number
- FRED returns the original print, not the revision
- unknown debt leaves enterprise value missing rather than treating it as zero

**The suite can never touch your real cache.** `conftest.py` points it at a
temporary directory before anything imports finlake, and refuses to run at all
if the path still resolves to `~/.finlake`. That guard exists because an
earlier version of the suite did delete a production cache, with every test
passing.

---

## Honest limitations

**`universe()` is a filing-activity proxy, not index membership.** It uses
`first_filed <= as_of` and `last_filed >= as_of − 200 days`. That's a
defensible *investable universe* and strictly better than applying today's
ticker list to 2015 — but a company can be listed without being in the S&P 500.
The bundled `universe/sp500_ndx.csv` is *current* membership with no add/drop
dates, so applying it to a past date reintroduces survivorship bias.

**Ticker history before your first build is inferred.** The SEC's ticker file
is a current snapshot with no history. Each mapping's validity window is
extended back to the company's first filing, and every such window is marked
`valid_from_inferred = 1`, so an inferred window is never mistaken for an
observed one. A ticker reused by a different company is the case this gets
wrong.

**A company that re-registers under a new CIK loses its history.** A
holding-company reorganization mints a new SEC identifier, and the old filings
stay under the old one. finlake follows one CIK per ticker, so the successor
shows only what it has filed since. Exxon Mobil is the current case: its 2026
re-registration leaves it with only a few quarters of fundamentals.

**Debt is the least reliable line.** Filers describe borrowings in more ways
than any other balance-sheet item, and some use company-specific XBRL elements
outside the us-gaap taxonomy. Where no debt figure resolves, enterprise value
and every EV multiple are left **missing** rather than computed as if debt were
zero; where only part of it resolves, `health` reports it. Enterprise value
excludes operating-lease liabilities, which many data providers include.

**Market data comes from an unofficial source** (Yahoo Finance, via yfinance).
There is no contract: fields change between releases, and polling too hard gets
the IP blocked. Requests are rate-limited and batched for that reason.

**Pre-2009 has no XBRL.** Structured data starts with the 2009 mandate and only
reaches smaller filers around 2011. Ten years back from today is fine; twenty
is not.

**Delisting dates are inferred.** A company that stops filing is treated as
gone after the grace period. Acquisitions and bankruptcies are not
distinguished, and the exact delisting date is approximate.

Comments in the code cite design decisions as `ISSUE-nnn`. Those refer to
the author's private design log, which is not part of this repository; the
comments are written to stand on their own.

---

## License

MIT — see [LICENSE](LICENSE).
