"""
LESSON 1 — What is a database, and why not just use a spreadsheet?
=================================================================

Run me:   python learn/01_what_is_a_database.py
Needs:    nothing. sqlite3 ships with Python.

A database is a file that holds tables and can answer questions about them
fast. SQLite is a database that lives in ONE FILE on your laptop. No server,
no install, no account. Python has it built in.

Think of it as Excel with three differences:
  1. It holds 200 million rows without dying.
  2. You ask it questions in a language called SQL instead of clicking.
  3. It can find one row out of 200 million in about a millisecond,
     because you can build an INDEX (like a book's index).

That's it. That's the whole concept.
"""

import sqlite3

# ---------------------------------------------------------------------------
# STEP 1: open a database. ":memory:" means "don't even write a file, just
# keep it in RAM" — perfect for learning. Change it to "test.db" and it
# becomes a real file on disk you could open again tomorrow.
# ---------------------------------------------------------------------------
conn = sqlite3.connect(":memory:")

# ---------------------------------------------------------------------------
# STEP 2: create a table. This is like naming your columns in Excel, except
# you also declare what TYPE each column is. TEXT = words, REAL = decimal
# number, INTEGER = whole number.
# ---------------------------------------------------------------------------
conn.execute("""
    CREATE TABLE revenue (
        ticker      TEXT,
        period_end  TEXT,
        value       REAL
    )
""")

# ---------------------------------------------------------------------------
# STEP 3: put rows in. The "?" marks are placeholders — you never paste
# values directly into SQL text (that's how you get SQL-injection bugs).
# executemany() inserts a whole list at once.
# ---------------------------------------------------------------------------
rows = [
    ("AAPL", "2023-03-31",  94836),
    ("AAPL", "2023-06-30",  81797),
    ("AAPL", "2023-09-30",  89498),
    ("AAPL", "2023-12-31", 119575),
    ("MSFT", "2023-03-31",  52857),
    ("MSFT", "2023-06-30",  56189),
    ("MSFT", "2023-09-30",  56517),
    ("MSFT", "2023-12-31",  62020),
]
conn.executemany("INSERT INTO revenue VALUES (?, ?, ?)", rows)
conn.commit()   # commit = "save it for real"

# ---------------------------------------------------------------------------
# STEP 4: ask questions. This is SQL. Read it out loud — it's close to English.
# ---------------------------------------------------------------------------
print("=" * 62)
print("Q: Show me all of Apple's quarters.")
print("=" * 62)
for r in conn.execute(
    "SELECT period_end, value FROM revenue WHERE ticker = 'AAPL' "
    "ORDER BY period_end"
):
    print(f"  {r[0]}   ${r[1]:>7,.0f}M")

print()
print("=" * 62)
print("Q: What was each company's total 2023 revenue?")
print("=" * 62)
# GROUP BY = 'collapse all rows that share a ticker into one row'
# SUM()    = 'and add up this column while you do it'
for r in conn.execute(
    "SELECT ticker, SUM(value) FROM revenue GROUP BY ticker ORDER BY 2 DESC"
):
    print(f"  {r[0]:<6} ${r[1]:>8,.0f}M")

print()
print("=" * 62)
print("Q: Which quarters were above $80B?")
print("=" * 62)
for r in conn.execute(
    "SELECT ticker, period_end, value FROM revenue WHERE value > 80000 "
    "ORDER BY value DESC"
):
    print(f"  {r[0]:<6} {r[1]}   ${r[2]:>7,.0f}M")

# ---------------------------------------------------------------------------
# STEP 5: an INDEX. This is the thing that makes databases fast.
#
# Without an index, "find AAPL" means reading every single row and checking.
# With an index, SQLite keeps a sorted lookup table on the side — same idea
# as the index at the back of a textbook. You go straight to the page.
#
# On 8 rows it makes no difference. On 300 million rows it's the difference
# between 4 minutes and 2 milliseconds. That is not an exaggeration.
# ---------------------------------------------------------------------------
conn.execute("CREATE INDEX idx_ticker ON revenue (ticker, period_end)")

print()
print("=" * 62)
print("What SQLite does with vs. without the index")
print("=" * 62)
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT * FROM revenue WHERE ticker='AAPL'"
).fetchone()
print(f"  {plan[-1]}")
print("  ('SEARCH ... USING INDEX' = good. 'SCAN' = reading every row.)")

print("""
TAKEAWAY
--------
A database is a file + tables + SQL + indexes. That's all `store.py` is
doing in finlake — it just declares more tables, with more columns, and
more indexes. Nothing in it is more complicated than what you just ran.
""")

conn.close()
