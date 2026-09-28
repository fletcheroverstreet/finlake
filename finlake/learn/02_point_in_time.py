"""
LESSON 2 — The one idea the whole project is built on
=====================================================

Run me:   python learn/02_point_in_time.py
Needs:    nothing.

THE PROBLEM, in one story
-------------------------
It's March 2019. You run a stock screen: "buy companies with revenue above
$1,000M." Company X reported $1,050M for 2018, so your screen buys it.

Two years later, in 2021, Company X restates. Turns out 2018 revenue was
really $900M — they'd booked revenue they shouldn't have.

Now you download data today to backtest your screen. You pull 2018 revenue
for Company X and get... $900M. So your backtest says the screen DIDN'T buy
it. But in real life it WOULD have. Your backtest just quietly dodged a
disaster it had no way of dodging.

Do that across thousands of companies and your backtest shows fantastic
returns that you could never actually have earned. This is called LOOKAHEAD
BIAS, and it's the single most common reason a strategy works on paper and
loses money live.

THE FIX
-------
Store BOTH dates on every number:

    period_end  = what period the number describes   (2018-12-31)
    filed       = when the number became public      (2019-02-01)

Never overwrite. When the restatement arrives, it's a NEW ROW, sitting next
to the original. Then "what did I know in March 2019?" is just:

    WHERE filed <= '2019-03-14'

Same table. Same query. One changed number. That's the entire idea.
"""

import sqlite3

conn = sqlite3.connect(":memory:")

# Note the two date columns. This is the whole design.
conn.execute("""
    CREATE TABLE facts (
        ticker      TEXT,
        period_end  TEXT,   -- what period this number describes
        filed       TEXT,   -- when this number became public  <<< THE KEY
        value       REAL,
        form        TEXT
    )
""")

conn.executemany("INSERT INTO facts VALUES (?,?,?,?,?)", [
    # Company X's 2018 revenue, as ORIGINALLY reported in Feb 2019
    ("X", "2018-12-31", "2019-02-01", 1050, "10-K"),
    # ...and as RESTATED in 2021. The original row is still there. Untouched.
    ("X", "2018-12-31", "2021-02-15",  900, "10-K/A"),

    # Company Y IPO'd in 2021. When it IPO'd, it filed 2018 financials as
    # historical background. Those numbers exist in the data — but nobody
    # could see them in 2019, because the company wasn't public yet.
    ("Y", "2018-12-31", "2021-06-30", 2000, "10-K"),
])
conn.commit()

# ---------------------------------------------------------------------------
# THE QUERY. Read it inside-out:
#
#   ROW_NUMBER() OVER (PARTITION BY ticker, period_end ORDER BY filed DESC)
#
#   "Group the rows by company+period. Inside each group, sort newest-filed
#    first, and number them 1, 2, 3..."
#
# Then keep only rn = 1, i.e. the newest version that existed on as_of.
# ---------------------------------------------------------------------------
QUERY = """
    WITH ranked AS (
        SELECT ticker, period_end, filed, value, form,
               ROW_NUMBER() OVER (
                   PARTITION BY ticker, period_end
                   ORDER BY filed DESC
               ) AS rn
        FROM facts
        WHERE filed <= ?          -- <<< the point-in-time bound
    )
    SELECT ticker, period_end, value, filed, form
    FROM ranked WHERE rn = 1
    ORDER BY ticker
"""


def screen(as_of, threshold=1000):
    print(f"\n{'='*66}")
    print(f"Running the screen on {as_of}  (buy if revenue > ${threshold}M)")
    print(f"{'='*66}")
    rows = conn.execute(QUERY, (as_of,)).fetchall()
    if not rows:
        print("  (no data was public yet)")
        return
    for tkr, pe, val, filed, form in rows:
        verdict = "BUY " if val > threshold else "pass"
        print(f"  {verdict} {tkr}  2018 revenue ${val:>6,.0f}M   "
              f"(filed {filed} on {form})")


# Standing in March 2019 with only what was public then:
screen("2019-03-14")
print("""
  -> Bought X at the ORIGINAL $1,050M. Correct: that's what the filing said.
  -> Y is completely absent. It hadn't IPO'd. It cannot contaminate the test.""")

# Downloading data today, the naive way:
screen("2026-08-07")
print("""
  -> Now X shows $900M and the screen skips it. Y shows up with 2018 numbers
     it back-filled at IPO. BOTH are lookahead bias. This is the fake result.""")

print(f"""
{'='*66}
The difference between those two outputs is the difference between a
backtest you can trust and a backtest that's fiction.

And the only thing separating them is one column (`filed`) and one line
of SQL (`WHERE filed <= ?`).

That is why I merged your "Tier 0 Data Layer" and "Tier 0 Point-in-Time
Store" into one module. The PIT store isn't a second project you build
after the first. It's a column you either put in on day one, or spend a
weekend retrofitting later.
{'='*66}
""")

conn.close()
