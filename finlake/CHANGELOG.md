# Changelog

## 0.13.0 — 2026-08-13 — what the loop refreshes, and what debt it finds

Started from a daemon printing several thousand HTTP 404 bodies per sweep.
The 404s were the visible end of a refresh loop maintaining the wrong list of
symbols; pulling on that surfaced four more failures of the usual kind — no
exception, no log line, a plausible number on screen.

### Fixed — the refresh loop

- **The loop swept 711 symbols to keep a 503-name index current.** The
  universe came from `ticker_map`, which lists every symbol the SEC maps to a
  filer CIK — so Allstate's four preferred series, Public Storage's fifteen,
  `OXY-WT` and `AIZN` were all being refreshed on all five tiers. That is 209
  instruments with no fundamentals at the market provider, about a thousand
  wasted requests per market sweep against a source that answers volume by
  blocking the IP, and nothing downstream that could ever display them. The
  refreshed universe is now declared explicitly (`universe` table,
  `python -m finlake universe --file`), and printed at daemon start-up.
- **Twelve full-history price requests were wasted every three minutes,
  permanently.** A symbol the provider has no history for writes no parquet,
  so it was still "missing" on the next pass — about 5,760 requests a day
  spent on fourteen names already known to return nothing. Worse, the backfill
  queue is in ticker order and its budget is twelve, so those fourteen
  permanently occupied the front of it and a genuine new listing sorting after
  them would never have been fetched at all. Outcomes are now recorded and a
  written-off symbol is left alone for 24 hours — never while the provider is
  failing wholesale.
- **AEP was absent from the entire cache** — zero rows in `facts`,
  `securities`, `ticker_map` and `filings`, one of 503 index members simply
  missing from every screen. Three faults in a row: the builder never passed
  the universe file's name column to the EDGAR company-search fallback (which
  matches on names, so it searched for "AEP" and found nothing); a CIK
  resolved that way was never written back to `ticker_map`; and once written,
  `load_ticker_map` closed it again on the next run for being absent from a
  file it was never in. Mappings now carry a `source`, and only SEC-sourced
  ones are closed when they vanish from the SEC's file.
- **The provider's own logging reached the console.** Every endpoint it has no
  data for is logged at ERROR on a handler-less logger, so Python's last-resort
  handler printed the raw 404 body four or five times per symbol. Suppressed —
  and replaced with a per-tier coverage line ("486 loaded, 3 no coverage"), so
  a source that has stopped answering is more obvious than it was before, not
  less.

### Fixed — debt, and the valuations built on it

- **Unknown debt was treated as zero debt.** `total_debt.fillna(0)` inside the
  enterprise value formula gave every filer whose debt no tag resolved an EV of
  `market cap − cash`, stated without qualification: **Ford $24.9bn against
  ~$197bn**, KKR $48.7bn against ~$101bn, AES $8.7bn against ~$49.5bn. EV/EBITDA,
  EV/EBIT and EV/Sales followed, Ford's off by a factor of eight on a screen
  where it sorted as the cheapest name in its sector. EV is now missing when
  debt is unknown.
- **A partial resolution defeated the combined-total fallback.** Oracle tags
  its current maturities under `NotesPayableCurrent` ($7.2bn) and everything
  else under `DebtLongtermAndShorttermCombinedAmount` ($129.5bn); one leg
  resolving was taken as the whole answer, an eighteen-fold understatement
  reaching net debt, debt/equity, interest coverage and Altman Z. The larger
  of the summed legs and the combined total now wins — a choice between two
  figures, never a sum, so nothing is double-counted.
- **A share count of zero produced a market cap of zero**, and with it a P/E,
  P/S and P/B of exactly 0.0 — the cheapest possible value on all three,
  sorting straight to the top of a value screen. Carvana carries `0` for
  eighteen quarters. `_safe_div` already refused to divide *by* a non-positive
  market cap, so earnings yield was NaN on the same row that reported a P/E of
  zero; the judgement is now applied at the source.
- **Altman Z silently dropped its solvency term for 140 of 503 names.**
  `Liabilities` is a subtotal many filers omit, Amazon and AMD among them. It
  is now recovered from assets − equity — the balance sheet identity
  rearranged — for that term only, deliberately not in `statements.periods`,
  where deriving one side of the identity would make
  `quality.check_balance_sheet_identity` pass trivially on all 140.

### Added

- `scripts/audit_valuation.py` — the two universe-wide invariants that were
  previously run by hand, plus a third that would have caught the debt bug:
  our enterprise value reconciled against the provider's. The identity holds
  *vacuously* when a leg is absent, which is exactly how a real error sat
  behind two clean checks. Currently 462/462, 500/500 and 398/406.
- `quality.check_debt_completeness` — surfaces debt that resolved only partly,
  from the filings alone: a short leg with no long-term element and no
  combined total, or an implied borrowing rate above 25% (Boston Properties
  paid $0.63bn of interest on $0.75bn of resolved debt). Reported rather than
  corrected: the tag that would fix Realty Income, `NotesPayable`, is a
  *component* for other filers, and promoting it trades an understatement here
  for an overstatement there.

### Known and deliberate

- Our enterprise value excludes operating lease liabilities; most providers
  include them. A lease-heavy retailer or casino therefore sits legitimately
  below the provider's figure — Dollar General and MGM reconcile to within a
  rounding error once leases are added back.
- EV is not reconciled for banks, brokers and insurers. Deposits and policy
  reserves are the raw material of the business, not borrowings, so "market cap
  plus net debt" is a category error there — Citigroup comes back at $34.7bn
  against a $180bn market cap, Berkshire at −$234bn.

## 0.12.0 — 2026-08-10 — accuracy pass: live prices, fiscal years, sentiment

Every fix below produced a plausible-looking wrong number rather than an
error. None of them raised, none logged, and the test suite passed throughout.

### Fixed — the headline price, and everything built on it

- **The header price was the close on the last FISCAL QUARTER END.** That is
  the only price a point-in-time ratio history ever pairs with the newest
  fundamentals row — correct for a history, wrong for "Price". On 10 August
  the company page read **$373 for Microsoft** beside a chart whose own last
  point was **$509**: a 27% error that propagated into market cap (a trillion
  dollars light), enterprise value, and every valuation multiple on the page.
  Added `finlake.quote()` / `quotes()` and `ratios.live_valuation()`. The
  header now reads the same cached series the chart draws, so the two cannot
  disagree, and both carry the bar's date.
- **The screener's market caps were stale by up to a quarter**, by a different
  amount for every company. That reached the market-cap band filter (names
  sorted into the wrong size bucket), the maximum-P/E filter, and the total
  market cap at the top of the page. The stored run snapshot stays
  point-in-time; `lodestar.ui.data.reprice` rescales the price-linear columns
  at read time, which is exact arithmetic rather than a re-derivation.
- **Enterprise value could come out BELOW market cap** on a company with
  positive net debt. `latest()` composes its dict column by column, taking
  each one's newest non-missing value, so market cap, total debt and cash
  could come from three different balance-sheet dates. Levels are now carried
  forward to the live row and the headline block is computed together.
- **A historical run is never re-priced at today's close.** That would be
  exactly the lookahead the `filed` column exists to prevent.

### Fixed — fundamentals

- **Annual columns belonged to the wrong fiscal year for most of the
  universe.** The year-end month was inferred as the most common month among
  period ends — but quarters land in four months, each appears equally often,
  and the tie fell to hash-table order. Microsoft resolved to September
  against a real June, Walmart and Nvidia to October against January, Costco
  to November against August. Every "FY2025" was a rolling four-quarter window
  matching no filing: **Microsoft's FY2025 revenue read $293.8bn against a
  filed $281.7bn.** Now taken from `securities.fiscal_year_end`, with a
  ±20-day tolerance for 52/53-week filers. Verified to the penny against filed
  figures for AAPL, MSFT, COST, KO, NVDA and WMT.
- **Revenue resolved to a COMPONENT of revenue for 79 companies.**
  `RevenueFromContractWithCustomer*` led the priority list, but rent, interest
  and premium income are not contract-with-customer revenue — so the tag
  resolved, returned a real number, and understated the top line:
  **AvalonBay $0.01bn against a filed $3.04bn, American Tower $0.94bn against
  $10.64bn, Humana $5.83bn against $129.66bn.** It reached revenue, P/S,
  EV/Sales, every margin, asset turnover and DSO. `Revenues` now leads, with
  `RevenuesNetOfInterestExpense` ahead of it for lenders.
- **Debt went missing for a seventh of the universe.** Phillips 66 showed no
  debt against $19bn on its balance sheet; General Motors and Oracle, which
  tag only a combined line, came through with enterprise value equal to market
  cap. Added the finance-lease and combined-debt elements, the latter used
  only as a fallback so it cannot double-count `debt_short`.
- **Total operating expenses omitted SG&A** for every filer reporting selling
  and G&A separately. The SG&A row displayed a composed figure while the total
  directly above it summed the raw column and showed R&D alone — Microsoft's
  FY2021 total read $16.9bn against a reported $45.9bn.
- **ROIC divided by a residual.** Invested capital is debt plus equity minus
  cash, so for a company that has bought its equity down it is the small
  difference between large numbers: United Airlines returned **6,416%**,
  McKesson 1,579%. The denominator must now be at least 2% of total assets.
- **Incomplete fiscal years are marked `(nQ)`.** The newest year is always
  still running, and a nine-month revenue figure printed beside four-quarter
  ones is understated by a quarter and looks entirely ordinary.

### Fixed — news sentiment

- **Routine 8-K filings were the most negative "news" in the database.** Item
  5.02's official SEC title is "**Departure** of Directors or Certain
  Officers; Election of Directors; Appointment of Certain Officers" — board
  elections and compensation boilerplate, and the most common 8-K there is.
  Scored as prose, "departure" was the only sentiment word present, so
  **~19,600 filings sat at -1.00**, at the highest source weight in the
  system. The SEC feed averaged **-0.99** while every other feed averaged
  about +0.5. Filings are now read from their 8-K item codes: 97% are
  procedural and carry no tone at all, and only genuinely directional items
  (non-reliance, bankruptcy, delisting, impairment) get one.
- **One word decided the whole headline.** `(pos - neg) / (pos + neg)` returns
  ±1 whenever a single sentiment word is found and none disagree, which in an
  eight-word headline is the normal case: **85% of every scored article sat on
  exactly +1.00 or -1.00.** Tone is now scaled by how much evidence produced
  it, and that figure is **0%**.
- **Words that were never opinions.** "Outstanding" was positive, and "shares
  outstanding" appears in a large fraction of all headlines; "record" was
  positive, in "record loss"; "fine" was negative, as an ordinary adjective;
  "advance" was positive, inside "Advanced Micro Devices". Every removal is
  recorded in `lexicon.py` with what it was matching.
- **Idioms read backwards.** "Failed to beat estimates" contains a positive
  word; "cuts costs" a negative one. Phrase patterns now claim spans
  exclusively, so a negation outranks the positive form it contains.
- **An explicit positive / neutral / negative label**, with a deadband, shown
  as a word rather than a bare signed number.
- `python -m finlake rescore-news` re-applies the current scoring to every
  stored article — tone is stored alongside each article, so a scoring change
  otherwise reaches new articles only.

### Fixed — the test suite was destroying the cache

- **Running `pytest` deleted 15.5 million facts from `~/.finlake`.** Test
  modules set `FINLAKE_HOME` to a temp directory before importing finlake,
  which looks airtight and is not: `config` resolves `DATA_DIR` once, at first
  import, so only the FIRST module to import finlake decides the path. And
  `smoke_test.py`, at the repo root, matches pytest's default `*_test.py`
  glob, defines no test functions so never appears in the collected list, and
  imports finlake at module level with no `FINLAKE_HOME` set. That import
  bound `DATA_DIR` to the real cache, and every `DELETE FROM` in the suite
  then ran against production: all facts, securities and ticker_map rows,
  188,000 macro observations, 67,000 news articles, and the market snapshot
  tables. **All 133 tests passed.** Fixed with `conftest.py` in both repos —
  set before any collection, plus a hard refusal to run if the path still
  resolves to the real cache — and `testpaths` scoped to `tests/`.

### Changed

- **Refresh cadence.** `task_quotes` described itself as batched and looped
  one request per ticker; 600 serial round trips do not fit in a 15-minute
  window, so the sweep never finished cleanly, while generating exactly the
  traffic pattern most likely to be throttled. Now genuinely batched at 40
  symbols per request — about 15 requests for the whole universe — and the
  tier runs every **3 minutes** instead of 15. `market` moves from daily to
  6-hourly.
- **Market snapshots stopped being silently discarded.** `market_snapshot`,
  `estimates`, `analyst_targets`, `short_interest`, `ownership`,
  `recommendations` and `estimate_trend` are keyed on `(ticker, as_of)` where
  `as_of` is a DATE, and were written with `INSERT OR IGNORE` — so the first
  capture of the day won and every later refresh that day was dropped. The
  write reported success and the price never moved. Snapshots still accumulate
  across days; within a day the newest capture wins, and `captured_at` records
  when it was taken.
- **Dividend yield is stored as a fraction.** The provider now returns
  `dividendYield` as a percentage (Coca-Cola arrives as 2.44) while every
  yield finlake computes is a fraction and the UI multiplies by 100 — so a
  2.4% yield rendered as 244%.
- **`quote_cache`**, a derived index of the price parquets, so the screener
  does not open 500 files per page load (6.9s to 0.02s). Every row records the
  source file's mtime and size and is discarded when either changes, so it can
  never serve a price the parquet disagrees with.

### Added

- **Company logos beside every ticker.** Fetched once from each company's own
  website (already stored on the profile), normalised to a square transparent
  PNG, and cached in `~/.finlake/logos` — 496 of 502 names, 1.5 MB on disk,
  refreshed weekly for anything new. The six without one get a monogram in
  their own stable colour, which reads as a design decision rather than as a
  gap. Every mark sits on the same light rounded tile, because corporate
  logos are drawn for white paper: some are near-black wordmarks that vanish
  on navy and some are pure white that vanish on anything pale, and one
  consistent tile is what turns 500 pieces of someone else's art direction
  into a uniform element of the layout.
- **`python -m lodestar daemon` — one command that keeps everything current.**
  Keeping the hub up to date took two separate things and only one was
  automated: the finlake refresh daemon kept prices, news and filings fresh,
  but nothing triggered `lodestar run`, so the composite scores, the ratings
  and the whole screener aged silently beside live prices. The new loop drives
  both, re-scoring once a day after the close, and reaches finlake only
  through `adapter.py` so the dependency direction is unchanged.
  `finlake.refresh_once()` / `refresh_status()` are now part of finlake's
  public surface for it.
- **Analyst targets carry their horizon.** A published price target is a
  12-month target by convention, and the header said only "Analyst target"
  with an upside percentage — which reads as "worth this now". Now shows the
  12-month consensus with its high/low range and analyst count, an explicitly
  interpolated 6-month waypoint (compounded, not halved), the recommendation
  scale, and days to next earnings.
- **Staleness is stated rather than implied** — the quote's bar date, the
  fundamentals' period end, and a warning when either is older than it should
  be.

## 0.11.0 — 2026-08-10 — stop double-adjusting splits

- **Every bar before a split was divided by the split ratio a second time.**
  The price cache was documented as raw, unadjusted OHLCV, but the provider
  always applies splits retroactively — `auto_adjust` controls dividends
  only. So `adjust="split"` applied the factor again: NVDA's 2024-05-21 close
  of $95.39 came back as $9.54. It reached historical price charts, every
  historical valuation multiple, VWAP (and through it buyback timing), and the
  12-1 momentum return whenever its window spanned a split.
- The adjustment modes now describe what they do: `split` (as cached),
  `total` (dividends reinvested), `as_traded` (undoes the provider's split
  adjustment to recover the printed price), and `none` (an alias of `split`).
  An unknown mode raises instead of falling through to a default.
- Market cap uses `as_traded`, so a price and the share count reported at the
  time sit on the same side of a split. NVDA's April 2024 market cap had read
  $21.6bn against a real ~$2.2tn.

## 0.10.0 — 2026-08-09 — data quality monitor

`python -m finlake health`. A failed check never silently corrects a number;
it flags it.

- **Internal consistency:** assets = liabilities + equity, the cash-flow
  sections sum to the reported change in cash, revenue − cost = gross profit,
  share counts are positive and diluted ≥ basic.
- **Completeness:** coverage per concept per period, gaps inside an otherwise
  continuous history, and staleness judged against each company's own filing
  lag rather than a fixed calendar.
- **Cross-source reconciliation:** SEC-derived figures against the market
  provider's own. Neither is assumed correct.
- Where the filer's own total balances but our components fall short, the
  finding is a concept gap rather than a wrong number. Coverage checks are
  sector-aware, so banks are not flagged for lines they never report.

## 0.9.0 — 2026-08-09 — live refresh

`python -m finlake refresh --daemon | --once | --status`. Each source refreshes
on the cadence it actually changes at; a machine that was off catches up on
start; a failing task is recorded without stopping the others, and a failed run
never counts as a success. The cadences have since been retuned — see
`refresh.py`.

## 0.8.0 — 2026-08-09 — news on the public API

`finlake.news()` and `finlake.news_signal()`, so lodestar reaches news through
the public surface rather than importing `finlake.sources`. `news_signal` is
batched: one pass over the news table for a whole universe.

## 0.7.0 — 2026-08-09 — hub rebuild, Phase 1e: macro and the real universe

### Added

- **FRED macro is live.** 22 series, ~185,000 observations: the full yield
  curve (3M/2Y/10Y/30Y plus the 10Y-2Y and 10Y-3M spreads), fed funds,
  mortgage rates, CPI and core CPI, core PCE, unemployment, payrolls, GDP,
  industrial production, housing starts, retail sales, consumer sentiment,
  VIX, high-yield spreads, WTI, and the dollar index.
- **`scripts/build_universe.py`** — scrapes the S&P 500 constituent list,
  resolves every name to a CIK, and writes `universe/sp500_ndx.csv`. Result:
  **503 tickers, all 503 resolved, spanning all 11 GICS sectors.**
- **`sec.resolve_cik_by_name()`** — CIK lookup via EDGAR company search.

### Fixed

- **`.env` was never read.** `.env.example` has always said to copy it and
  put credentials there, but nothing loaded the file — every credential also
  had to be exported into the shell, and both failure modes were silent (a
  correct FRED key in `.env` produced "Set FRED_API_KEY"; a missing SEC
  user-agent produced a 403). `config.py` now loads it, with real
  environment variables still winning.
- **Requesting vintages for daily market series returned HTTP 400.** DGS10
  has ~16,000 daily observations since 1962; asking ALFRED for every vintage
  of every one exceeds FRED's response limit. Split into `NON_REVISED` — and
  this is a modelling distinction, not an optimization. A Treasury yield is
  a market observation, printed once and never restated. An economic
  statistic is an estimate revised for years: **Q1 2020 real GDP was first
  published at 18,987.9 and reads 20,709.2 today.** Verified end to end —
  `GDPC1` carries 417 distinct vintages, `DGS10` exactly one.
- **The SEC's ticker files are not a complete registry.** Both
  `company_tickers.json` and `company_tickers_exchange.json` carry 10,398
  entries and neither contains AEP, an S&P 500 utility. Resolved by name
  instead. The same lookup finds Exxon's predecessor CIK 34088 — the fix for
  the CIK-continuity issue logged in 0.4.0.

### Known

- **The Nasdaq-100 list could not be scraped.** Wikipedia moved its
  constituents into a navbox template that carries company names but no
  tickers, and the ETF holdings file blocks automated requests. The S&P 500
  alone is 503 names across all 11 sectors, and the overlap with Nasdaq-100
  is large, so the practical gap is a handful of non-S&P names. The builder
  takes additional sources without code changes when a machine-readable one
  is found.

## 0.6.0 — 2026-08-09 — hub rebuild, Phase 1d: news

Multi-source news with financial sentiment and event classification. This
is the feed lodestar's News bucket was built for and has been shipping
`NullNewsProvider` against — 10% of the composite, excluded universe-wide
because no provider existed.

### Added

- **`sources/news.py`** — four sources behind one adapter interface: Yahoo
  Finance RSS, Google News RSS, the market provider's curated feed, and
  **SEC 8-K filings**. Each is isolated; a dead feed costs that feed and
  nothing else. All are published RSS/Atom — no paywall circumvention, no
  article-body scraping.
- **`lexicon.py`** — Loughran-McDonald financial sentiment scoring, with
  negation handling. Point the `FINLAKE_LM_DICTIONARY` env var at the full
  LM master CSV to replace the bundled subset.
- `news_articles` + `news_tickers` tables, and `news.signal()` — the
  event-weighted, exponentially-decayed aggregate per ticker.
- `--news` and `--all` flags on `scripts/build.py`.

### Why not a general-purpose sentiment model

Ordinary sentiment lexicons are wrong on financial text, and wrong in one
direction. Loughran and McDonald found roughly three quarters of the words a
standard psychological dictionary marks negative are not negative in a
filing at all — *liability*, *cost*, *tax*, *crude*, *depreciation* are
neutral accounting vocabulary. Score a filing with a general lexicon and you
mostly measure how much accounting it contains. There is a test asserting
exactly this sentence scores as no-signal.

### Three things done deliberately

**Deduplication is title-based, and that is the point.** A Reuters story
reaches Yahoo, Google News, and two aggregators within minutes. Counted once
each, "widely syndicated" becomes "five independent pieces of bad news" —
precisely the quantity the signal is trying to measure. Keying on URL
deduplicates nothing, since every syndicating site mints its own. Live
result: 339 articles fetched across two tickers, 306 unique.

**Sources are not equal evidence.** An 8-K is a company telling the SEC
something material, on a deadline, under penalty of law. A wire release is
the company's own framing. An aggregator headline is neither. A trusted
byline raises the weight of whatever feed carried it, so a Reuters story
counts as Reuters wherever it arrived from.

**Absence of news is reported as absence.** A ticker with no articles gets
`score=None, coverage=0.0`, never a fabricated neutral zero. Those are
different claims — one says the news was balanced, the other says we know
nothing — and a fake zero fed into a z-score puts a name we know nothing
about at exactly the sector average, which reads as a finding. The same
applies per article: an article containing no sentiment vocabulary counts
toward volume but is not scored.

### Fixed

- **Atom feeds parsed to nothing, silently.** Atom puts every tag in a
  default namespace, so `find("title")` matches nothing. The SEC 8-K feed
  fetched fine, parsed to 40 entries, and produced **zero** articles because
  every title lookup missed. Namespaces are now stripped at parse time so
  RSS and Atom are one shape downstream.
- **Every 8-K collapsed into a single row.** They all carry the identical
  title "8-K - Current report", so title-based dedupe reduced a company's
  entire filing history to one article — 40 filings became 1. Filings now
  key on their accession number. News still keys on title, because that is
  what makes cross-source dedupe work; the two rules coexist rather than one
  replacing the other.

## 0.5.0 — 2026-08-09 — hub rebuild, Phase 1c: market data

The first source that isn't a filing. Filings say what a company earned;
they say nothing about what it is worth today, what analysts expect, how
much of the float is short, or who owns it.

### Added

- **`sources/market.py`** and ten new tables: `market_snapshot`,
  `estimates`, `estimate_trend`, `analyst_targets`, `recommendations`,
  `earnings_history`, `short_interest`, `ownership`, `profile`,
  `earnings_calendar`.
- **Forward P/E is now computable.** The SEC publishes no estimates of any
  kind, so a forward multiple was impossible from filings alone.
  `forward_eps` is the field that unlocks it. Verified live: AAPL 29.8x
  forward vs 32.1x trailing, NVDA 16.2x vs 31.6x, MU 6.0x vs 20.7x.
- **Real analyst revision data** (`estimate_trend`): consensus EPS as it
  stood 7/30/60/90 days ago, plus counts of analysts revising each way.
  lodestar's `fundamental_revision` metric is documented as a *proxy*
  because "there is no consensus-estimates feed" — there is one now.
- Short interest (shares short, days to cover, % of float, prior month),
  insider and institutional ownership percentages, price targets,
  buy/hold/sell distributions, earnings surprise history, and
  sector/industry labels for peer grouping.

### Point-in-time, extended to estimates

Every market table is keyed on `as_of`, the date the value was **captured**.
An estimate is not a fact about a period — it is a fact about what analysts
believed on a date, and it is revised constantly. Storing only the current
value would let a backtest over last year silently use this morning's
consensus: the same lookahead the `filed` column prevents on the filings
side. Snapshots accumulate; nothing is overwritten.

The alignment rule matters more than it looks. A June quarter is only
reported in late July, so every estimate for it is published *after* the
period end. Aligning estimates strictly to the period end finds nothing at
or before June 30 and returns blank — for every ticker, forever. A period's
window instead runs until the next period end, still bounded by the query's
`as_of`.

### Fixed

- **`scripts/build.py` crashed immediately on Windows.** The progress output
  contains `→`, which cp1252 cannot encode, so the build died with a
  `UnicodeEncodeError` on its first status line having done no work. stdout
  and stderr are now reconfigured to UTF-8.
- `build.py` now runs `backfill_ticker_validity` after loading submissions,
  so a fresh build produces working historical as-of queries rather than
  needing a manual follow-up step.

### Notes on the source

It is unofficial: fields appear, vanish, and change type between releases.
Every value goes through coercion that turns `''`, `'N/A'`, `NaN`, and
`Infinity` into NULL rather than into `0.0`; every section is isolated so one
dead endpoint costs that section and not the ticker. Requests are paced at
2/s through the same token bucket the SEC calls use — the limit is set by
what does not get blocked, not by what is documented, because being blocked
takes prices down entirely rather than merely making them stale.

Provider-computed multiples are stored **alongside** finlake's own, never
instead of them. Where the two disagree that is a finding for the
data-quality layer, not a reason to silently prefer one.

## 0.4.0 — 2026-08-09 — hub rebuild, Phase 0: correctness

Bug-fix release. No new concepts or sources yet — this is everything the
audit found wrong with what was already here, fixed first so the rebuild
isn't stacked on top of it. Every fix has a regression test
(`tests/test_concept_resolution.py`, `tests/test_http_cache.py`, and two
additions to `tests/test_prices.py`). 41 finlake tests, 221 lodestar tests,
all green.

Common thread worth naming: not one of these threw an error. Every single
one produced a plausible-looking number, or a quietly empty column, on real
data.

### Fixed — data correctness

- **A tag switch silently truncated history.** `fundamentals()` resolved
  each concept to a *single* XBRL tag, picking whichever candidate yielded
  the most quarters. Filers switch tags permanently mid-history, so this
  always drops one era. Live case: Apple tags revenue as `SalesRevenueNet`
  through 2018 and `RevenueFromContractWithCustomerExcludingAssessedTax`
  from 2017 on — the legacy tag won on count, and **AAPL's revenue column
  was blank for every quarter after 2018**. Candidate tags are now merged in
  CONCEPTS priority order: the highest-priority tag owns any period it
  covers, later tags fill only what's still empty. AAPL revenue goes from
  73 of 143 rows to 73 of 75, continuous 2006→2026.
- **Facts in different currencies were pooled into one series.** Facts were
  keyed by tag alone, so a filer tagging `Revenues` in both USD and EUR had
  both fed to the quarterizer, which would difference a USD year-to-date
  figure against a EUR one and emit the result as a quarter. Now keyed by
  (tag, unit), preferring the unit the concept is denominated in and falling
  back to the filer's own reporting currency.
- **Cover-page facts created phantom quarters.** dei's
  `EntityCommonStockSharesOutstanding` is dated at the filing, weeks after
  the quarter it ships with. Each such date became its own row with every
  other column empty — Apple's frame was 143 rows, half of them periods no
  company ever reported. Cover-page facts now snap back onto the most recent
  real reporting period, keeping the value (it's the freshest share count,
  which is what market cap wants) without inventing a period.
- **Dividends were adjusting historical volume.** With `adjust="total"`,
  volume was divided by the dividend-inclusive price factor. A split changes
  the share count; a dividend does not. Price and volume now carry separate
  factors. This fed VWAP, and VWAP feeds lodestar's buyback-timing metric.

### Fixed — history depth

- **`load_submissions` read only the last ~1,000 filings.** Everything older
  is paged into `filings.files`, which was never fetched. Consequence beyond
  a short filing index: `first_filed` is derived from this table and
  `pit.universe()` gates on `first_filed <= as_of`, so **the oldest and
  largest companies were being dropped from every historical screen.** AAPL:
  1,000 → 2,238 filings, `first_filed` corrected from 2015-06-01 to
  1994-01-26. A 21-year error.
- **Every historical `--as-of` run returned an empty universe.** The SEC
  ticker file is a current snapshot, so `load_ticker_map` opened every
  mapping with `valid_from = today` — meaning a mapping's history began the
  day you first ran the builder, and any earlier as-of resolved to no ticker
  at all. `pit.universe()` drops null-ticker rows, so historical runs
  returned nothing rather than something partial. New
  `sec.backfill_ticker_validity()` extends a window back to the company's
  first filing, applied **only** to tickers with exactly one mapping and no
  closed history (i.e. no evidence of reuse), and recorded per row via a new
  `ticker_map.valid_from_inferred` column so an inferred window is never
  mistaken for an observed one. lodestar's README documented this as
  "historical runs don't work yet"; they do now.
- **`load_prices` defaulted to a 2000-01-01 cutoff.** Now pulls full
  available history. Depth is one request either way.

### Fixed — HTTP layer

- **The negative cache never suppressed a request.** A 404 was stored as
  JSON `null`, which `_read_cache` returned as `None` — indistinguishable
  from "nothing cached" — so `get_json` treated it as a miss and re-requested
  every time. Over a universe walk that is one request per missing resource
  *per run*, which is what rate limits exist to stop. A recorded 404 is now a
  distinct sentinel, backward-compatible with existing cache entries.
- **Retry exhaustion reported `None` as the cause.** 429/5xx responses never
  set `last_err`, so the raised message named no reason. Also: a
  non-retryable status or a 200 whose body isn't JSON now breaks out
  immediately instead of burning the remaining attempts, and a non-numeric
  `Retry-After` (RFC 7231 permits an HTTP date) falls back to exponential
  backoff instead of crashing the build on a `float()` parse.
- **`_submission_rows` padding.** `primaryDocument` is missing from some
  older overflow files; `zip()` over ragged columns would have silently
  truncated an entire block to zero rows.

### Known, not yet fixed

- **History is lost across corporate reorganizations.** XOM resolves to CIK
  2115436 ("ExxonMobil Holdings Corp", first filing 2026-07-01) and returns
  6 quarters where peers return 75 — all of Exxon's history sits under
  predecessor CIK 34088, which is never fetched. The previous build logged
  this as ISSUE-002 and closed it as "no tag to add"; that diagnosis was
  wrong. Needs a CIK alias chain, tracked separately.

## 0.3.0 — 2026-08-08 — lodestar Phase C amendment

Everything the full 44-name lodestar smoke run surfaced that belonged in
finlake. Regression-diffed against a before-snapshot on the same pinned
20-ticker list: **zero unexpected differences** — every previously
existing value byte-identical, only the three new concept columns added.
The Micron check (`test_micron_real_shape`) still passes.

### Added

- Three new `CONCEPTS` entries, all confirmed present in cached data via
  direct SQL before adding:
  - `depreciation_amortization` (`DepreciationDepletionAndAmortization`)
    — unblocks lodestar's FCF/EBITDA fallback and reinvestment rate.
  - `shares_repurchased` (`StockRepurchasedDuringPeriodShares`) — the
    share COUNT, distinct from `buybacks` (the dollar amount, already
    mapped). Unblocks a per-share average repurchase price, which is
    what a buyback-timing analysis actually needs.
  - `goodwill_impairment` (`GoodwillImpairmentLoss`).
- **IFRS (`ifrs-full`) fallback tags** on the core concepts: `revenue`,
  `cost_of_revenue`, `operating_income`, `pretax_income`, `equity`,
  `cash`, `cfo`. `pit.py` already accepted 20-F/40-F forms, but every
  `CONCEPTS` entry listed only `us-gaap` tag names, so a foreign private
  issuer resolved almost nothing. See the honest limitation below for
  what this does and doesn't buy.

### Known limitation confirmed, not fixed (can't be)

- **Foreign private issuers that file 20-F annually still yield no
  quarterly history.** The IFRS tags above make their facts resolvable,
  but TSM (the case that surfaced this) publishes 5,480 facts on 20-F
  against 501 on 6-K, and its flow periods are 364-365 day full years
  carrying 200-450 facts each while individual quarters carry 1-3. There
  is no quarterly chain to difference — an absence of data at the source,
  not a parsing failure. New note:
  `ISSUE-005-foreign-filers-annual-only`.

## 0.2.0 — 2026-08-08 — lodestar Phase A amendment

Findings and full evidence in `FINLAKE-FINDINGS.md` (in the lodestar repo)
and DEC/ISSUE notes in this project's vault folder. Regression-diffed
against a 41-company snapshot before and after; only the changes below are
present, everything else is byte-identical. `tests/test_micron_real_shape`
still passes.

### Fixed

- **`operating_income`'s CONCEPTS fallback was mislabeled pretax income.**
  `IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems-
  NoncontrollingInterest` includes net interest; `OperatingIncomeLoss`
  doesn't. Every filer without a discrete operating-income line was
  silently getting pretax income scored as operating income — live for
  JPM, BAC, WFC, GS, MS, SCHW, O, PFE, XOM, JNJ, and KLAC. The fallback is
  removed; those filers now correctly return `operating_income` as missing
  rather than wrong. Pretax income is available on its own via the new
  `pretax_income` concept.
- **Negative derived quarters from divestiture-restatement scope mismatches
  are now dropped, not returned.** (ISSUE-001.) Scoped narrowly to
  `revenue` only. Fixes WDC's 2023-06-30 quarter.

### Added

- Nine new `CONCEPTS` entries: `shares_outstanding`, `interest_expense`,
  `pretax_income`, `tax_expense`, `goodwill`, `intangible_assets`,
  `accounts_receivable`, `accounts_payable`, `short_term_investments`.
- `finlake.vwap(ticker, start=, end=, adjust=, price=)` — dollar-weighted
  average price over a window, reusing the existing price-adjustment
  pipeline.
- `quarterize.quarterize_multi(rows, non_negative=False)` — opt-in guard,
  default preserves prior behaviour exactly.
- `quarterize.implausible_quarters(rows)` — reports what the guard above
  would drop, for diagnostics.
- `pyproject.toml` — packaging metadata; no behaviour change.
- `scripts/regression_snapshot.py` — the before/after diffing tool used to
  verify this release; kept for the next amendment window.

### Investigated, no fix needed

- **ISSUE-002 (XOM tags unmapped) — re-diagnosed.** XOM's ticker currently
  resolves to a CIK with ~274 total cached facts and a `first_filed` of
  2026-07-01. The tags it does report (`Revenues`,
  `NetCashProvidedByUsedInOperatingActivities`) were already in `CONCEPTS`.
  There was no tag to add — see the updated ISSUE-002 note.
- **ISSUE-004 (`universe()` is a filing-activity proxy)** — confirmed
  accurate, no code change; already correctly scoped to a future
  constituent-file project.

## 0.1.0 — 2026-08-07

Initial Tier 0 build. See this project's vault folder for the full
verification record.
