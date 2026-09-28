"""
LESSON 3 — Getting real numbers out of the SEC, with zero installs
==================================================================

Run me:   python learn/03_real_sec_data.py AAPL
Needs:    an internet connection. Nothing from pip — urllib is built in.

WHAT'S HAPPENING HERE
---------------------
You already know EDGAR as the website where you read 10-Ks. What you may
not know is that the SEC also publishes the same data as an API.

An API is just a URL that returns data instead of a webpage. You visit it
the same way you'd visit any website — but what comes back is structured
text meant for programs, not HTML meant for eyeballs.

The format is JSON. It looks like this:

    {"name": "Apple Inc.", "cik": 320193, "quarters": [1, 2, 3, 4]}

...which is exactly a Python dict. `json.loads()` turns the text into a
dict and you're done. That's the entire skill.

WHY THE DATA IS ALREADY TAGGED
------------------------------
Since ~2009 the SEC has required filers to tag every number in their
financial statements using a standard called XBRL. So Apple doesn't just
publish a PDF with "Revenue ... 94,836" — it publishes a machine-readable
record saying "the tag RevenueFromContractWithCustomerExcludingAssessedTax,
in USD, for the period 2023-01-01 to 2023-04-01, equals 94836000000."

That tagging is why this project is possible at all without paying
Bloomberg $24,000/year.
"""

import json
import ssl
import sys
import urllib.request

# The SEC REQUIRES a User-Agent header with a real contact address. Without
# one you get a 403 Forbidden. Put your actual email here.
UA = "finlake-learning/0.1 (your.email@example.com)"


def fetch(url):
    """Download a URL and parse the JSON into a Python dict."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode())


def find_cik(ticker):
    """Every filer has a CIK — a permanent SEC ID number. Tickers change;
    CIKs don't. So step one is always ticker -> CIK."""
    data = fetch("https://www.sec.gov/files/company_tickers.json")
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            return entry["cik_str"], entry["title"]
    raise SystemExit(f"Couldn't find ticker {ticker}")


def main(ticker="AAPL"):
    cik, name = find_cik(ticker)
    print(f"\n{ticker} = {name}   (CIK {cik})")

    # companyconcept returns ONE tag's full history. Much smaller than
    # companyfacts, which returns every tag the company has ever used.
    tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/"
           f"CIK{cik:010d}/us-gaap/{tag}.json")
    print(f"Fetching: {url}\n")

    try:
        data = fetch(url)
    except Exception:
        print(f"That tag isn't used by {ticker}. Trying the older 'Revenues'...")
        url = url.replace(tag, "Revenues")
        data = fetch(url)

    facts = data["units"]["USD"]
    print(f"Got {len(facts)} raw revenue facts. Here are the last 12:\n")
    print(f"{'period':<24} {'days':>5} {'value ($M)':>12} {'filed':<12} form")
    print("-" * 70)

    for f in facts[-12:]:
        start, end = f.get("start"), f["end"]
        days = ""
        if start:
            import datetime as dt
            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
        period = f"{start or '(instant)'} → {end}"
        print(f"{period:<24} {days:>5} {f['val']/1e6:>12,.0f} "
              f"{f['filed']:<12} {f['form']}")

    print(f"""
NOW LOOK AT WHAT YOU JUST PRINTED — this is the whole problem in one table.

1. The `days` column is not always ~90. You'll see 90-ish, 180-ish, 270-ish
   and 365-ish rows mixed together. Those longer ones are YEAR-TO-DATE, not
   quarters. If you naively treat every row as a quarter you triple-count.
   -> This is what `quarterize.py` fixes.

2. The same `period` appears more than once with different `filed` dates.
   Those are restatements and re-confirmations. Every row is a separate
   version of the same number.
   -> This is what `pit.py` fixes.

3. There's no row for a discrete Q4. There never is — Q4 comes from the
   10-K's full-year figure minus the nine-month figure.
   -> Also `quarterize.py`.

4. If the first tag 404'd and it fell back to 'Revenues', that's trap #4:
   there is no single universal tag for revenue.
   -> This is what the CONCEPTS map in `api.py` fixes.

Every ugly-looking line of code I wrote you exists because of something
visible in the table above.
""")


if __name__ == "__main__":
    if "your.email@example.com" in UA:
        print("!! Edit the UA variable at the top of this file first —\n"
              "   the SEC will reject the request without a real email.\n")
    main(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
