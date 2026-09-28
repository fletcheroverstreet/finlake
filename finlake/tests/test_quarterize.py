"""Quarterization tests. All offline, all synthetic.

Each test is a real filer pattern I've seen break naive parsers.
"""

import os
import tempfile

os.environ.setdefault("FINLAKE_HOME", tempfile.mkdtemp(prefix="finlake_test_"))

from finlake.quarterize import (  # noqa: E402
    classify_duration, implausible_quarters, quarterize_multi,
)


def dur(start, end, val, accn, filed, fy=2023, fp=None):
    return {"period_start": start, "period_end": end, "val": val,
            "accn": accn, "filed": filed, "fy": fy, "fp": fp}


def inst(end, val, accn, filed, fy=2023, fp=None):
    return {"period_start": None, "period_end": end, "val": val,
            "accn": accn, "filed": filed, "fy": fy, "fp": fp}


def test_duration_classification():
    assert classify_duration("2023-01-01", "2023-03-31") == "Q"
    assert classify_duration("2023-01-01", "2023-06-30") == "H"
    assert classify_duration("2023-01-01", "2023-09-30") == "9M"
    assert classify_duration("2023-01-01", "2023-12-31") == "FY"
    assert classify_duration(None, "2023-12-31") == "INSTANT"
    # A 5-month stub from a fiscal-year change: correctly rejected.
    assert classify_duration("2023-01-01", "2023-05-31") is None
    print("  ok  duration bucketing")


def test_ytd_chain_is_differenced():
    """The common case: filer tags YTD only. Q1=100, H1=250, 9M=430, FY=600.
    Correct quarters are 100, 150, 180, 170."""
    rows = [
        dur("2023-01-01", "2023-03-31", 100, "q1", "2023-04-20"),
        dur("2023-01-01", "2023-06-30", 250, "q2", "2023-07-20"),
        dur("2023-01-01", "2023-09-30", 430, "q3", "2023-10-20"),
        dur("2023-01-01", "2023-12-31", 600, "fy", "2024-02-01"),
    ]
    qs = quarterize_multi(rows)
    vals = [round(q.value, 2) for q in qs]
    assert vals == [100, 150, 180, 170], vals
    assert qs[0].derived is False, "Q1 was reported directly"
    assert all(q.derived for q in qs[1:]), "Q2-Q4 are differenced"
    print("  ok  YTD chain differenced correctly, Q4 derived from FY - 9M")


def test_q4_is_derived_when_absent():
    """There is no Q4 10-Q. Ever. If your parser expects one you lose 25% of
    every income statement."""
    rows = [
        dur("2023-01-01", "2023-03-31", 100, "q1", "2023-04-20"),
        dur("2023-01-01", "2023-06-30", 250, "q2", "2023-07-20"),
        dur("2023-01-01", "2023-09-30", 430, "q3", "2023-10-20"),
        dur("2023-01-01", "2023-12-31", 600, "fy", "2024-02-01"),
    ]
    qs = quarterize_multi(rows)
    q4 = [q for q in qs if q.period_end == "2023-12-31"][0]
    assert q4.value == 170, q4.value
    assert q4.derived is True
    print("  ok  Q4 = FY - 9M")


def test_discrete_quarters_preferred_over_derived():
    """Some filers tag discrete quarters. Use them; don't difference."""
    rows = [
        dur("2023-01-01", "2023-03-31", 100, "q1", "2023-04-20"),
        dur("2023-04-01", "2023-06-30", 150, "q2", "2023-07-20"),
        dur("2023-07-01", "2023-09-30", 180, "q3", "2023-10-20"),
        dur("2023-10-01", "2023-12-31", 170, "q4", "2024-02-01"),
    ]
    qs = quarterize_multi(rows)
    assert [q.value for q in qs] == [100, 150, 180, 170]
    assert not any(q.derived for q in qs), "should not have differenced"
    print("  ok  discrete quarters used as-is")


def test_instant_facts_are_never_differenced():
    """Assets at each quarter-end are levels, not flows. Differencing them
    turns a $5bn balance sheet into a $200m 'quarterly assets' number, which
    then quietly ruins every ratio you compute."""
    rows = [
        inst("2023-03-31", 5000, "q1", "2023-04-20"),
        inst("2023-06-30", 5200, "q2", "2023-07-20"),
        inst("2023-09-30", 5100, "q3", "2023-10-20"),
        inst("2023-12-31", 5400, "fy", "2024-02-01"),
    ]
    qs = quarterize_multi(rows)
    assert [q.value for q in qs] == [5000, 5200, 5100, 5400]
    assert not any(q.derived for q in qs)
    print("  ok  balance-sheet levels passed through untouched")


def test_missing_middle_quarter():
    """Filer skipped tagging H1. We should still get Q1 and a 9M-derived
    lump rather than crashing or silently emitting nonsense."""
    rows = [
        dur("2023-01-01", "2023-03-31", 100, "q1", "2023-04-20"),
        dur("2023-01-01", "2023-09-30", 430, "q3", "2023-10-20"),
        dur("2023-01-01", "2023-12-31", 600, "fy", "2024-02-01"),
    ]
    qs = quarterize_multi(rows)
    ends = [q.period_end for q in qs]
    assert "2023-06-30" not in ends, "must not invent a quarter it can't compute"
    # 430-100 spans TWO quarters. Emitting it as one quarter would corrupt
    # every margin downstream, so it is dropped rather than guessed at.
    assert "2023-09-30" not in ends, "must not emit a 2-quarter lump as a quarter"
    q4 = [q for q in qs if q.period_end == "2023-12-31"][0]
    assert q4.value == 170 and q4.derived is True
    print("  ok  2-quarter gap dropped instead of fabricated; Q4 still derived")


def test_micron_real_shape():
    """MU FY2025, exactly as EDGAR returns it. Q4 is never filed; it must come
    out as 37,378 - 26,063 = 11,315, and the four quarters must tie to the
    10-K full-year figure."""
    rows = [
        dur("2024-08-30", "2024-11-28",  8709, "a", "2025-01-08"),
        dur("2024-11-29", "2025-02-27",  8053, "b", "2025-03-21"),
        dur("2024-11-29", "2025-02-27",  8053, "b2", "2026-03-19"),  # comparative
        dur("2024-08-30", "2025-05-29", 26063, "c", "2025-06-26"),
        dur("2024-08-30", "2025-05-29", 26063, "c2", "2026-06-25"),  # comparative
        dur("2025-02-28", "2025-05-29",  9301, "e", "2025-06-26"),
        dur("2024-08-30", "2025-08-28", 37378, "f", "2025-10-03"),
    ]
    qs = quarterize_multi(rows)
    got = {q.period_end: q for q in qs}
    assert round(got["2025-08-28"].value) == 11315, got["2025-08-28"].value
    assert got["2025-08-28"].derived is True
    assert round(got["2025-05-29"].value) == 9301   # reported beats derived lump
    assert round(sum(got[e].value for e in
                     ["2024-11-28", "2025-02-27", "2025-05-29", "2025-08-28"])) == 37378
    print("  ok  Micron FY2025 reconstructs and ties to the 10-K")


def test_restated_fact_in_same_slot_uses_latest_filed():
    rows = [
        dur("2023-01-01", "2023-03-31", 100, "orig", "2023-04-20"),
        dur("2023-01-01", "2023-03-31", 110, "amend", "2023-11-01"),
    ]
    qs = quarterize_multi(rows)
    assert len(qs) == 1 and qs[0].value == 110
    print("  ok  later-filed version wins within a slot")


def test_fiscal_year_not_calendar_year():
    """Apple's FY2023 ends 2023-09-30. Grouping by calendar year splits it.
    Grouping by period_start handles it with no special case."""
    rows = [
        dur("2022-10-01", "2022-12-31", 100, "q1", "2023-02-01", fy=2023),
        dur("2022-10-01", "2023-04-01", 250, "q2", "2023-05-01", fy=2023),
        dur("2022-10-01", "2023-07-01", 430, "q3", "2023-08-01", fy=2023),
        dur("2022-10-01", "2023-09-30", 600, "fy", "2023-11-01", fy=2023),
    ]
    qs = quarterize_multi(rows)
    assert [round(q.value) for q in qs] == [100, 150, 180, 170]
    assert all(q.fy == 2023 for q in qs)
    print("  ok  grouped by fiscal year, not calendar year")


def _wdc_style_divestiture_rows() -> list[dict]:
    """Shape of ISSUE-001: a 9M figure from BEFORE a divestiture restatement
    paired with a FY figure from AFTER it. Different scopes, same fiscal
    year, so FY - 9M goes negative even though revenue can never actually be
    negative. This is the real WDC/SanDisk-spinoff failure mode, reproduced
    with round numbers instead of WDC's actual filings."""
    return [
        dur("2023-01-01", "2023-03-31", 100, "q1", "2023-04-20"),
        dur("2023-01-01", "2023-09-30", 400, "9m-pre-divestiture", "2023-10-20"),
        dur("2023-01-01", "2023-12-31", 300, "fy-post-divestiture", "2024-02-01"),
    ]


def test_non_negative_default_preserves_prior_behaviour():
    """Backward compatibility: non_negative defaults to False, so any
    existing caller that doesn't pass it sees exactly the old (buggy)
    output. Fixing this had to be opt-in at the call site (api.py), not a
    silent change to quarterize_multi's default behaviour."""
    rows = _wdc_style_divestiture_rows()
    qs = quarterize_multi(rows)
    q4 = [q for q in qs if q.period_end == "2023-12-31"][0]
    assert q4.value == -100, q4.value
    print("  ok  non_negative=False (the default) still emits the negative quarter")


def test_negative_derived_quarter_dropped_when_non_negative():
    rows = _wdc_style_divestiture_rows()
    qs = quarterize_multi(rows, non_negative=True)
    ends = [q.period_end for q in qs]
    assert "2023-12-31" not in ends, "negative derived quarter should be dropped"
    assert "2023-03-31" in ends, "the real, non-negative Q1 must survive"
    print("  ok  non_negative=True drops the negative derived quarter, keeps the rest")


def test_non_negative_never_drops_a_reported_or_instant_value():
    """The guard only applies to DERIVED (differenced) quarters. A directly
    reported negative value (net income can legitimately be negative) or an
    instant balance-sheet value must never be dropped, even with
    non_negative=True — dropping a real loss would be a worse bug than the
    one this parameter fixes."""
    reported_loss = [
        dur("2023-10-01", "2023-12-31", -50, "q4", "2024-02-01"),  # discrete, not derived
    ]
    qs = quarterize_multi(reported_loss, non_negative=True)
    assert len(qs) == 1 and qs[0].value == -50 and qs[0].derived is False
    print("  ok  a directly-reported negative value is never dropped")


def test_implausible_quarters_reports_what_was_dropped():
    rows = _wdc_style_divestiture_rows()
    flagged = implausible_quarters(rows)
    assert len(flagged) == 1
    assert flagged[0].period_end == "2023-12-31"
    assert flagged[0].value == -100
    print("  ok  implausible_quarters() reports the dropped quarter for diagnostics")


if __name__ == "__main__":
    for fn in [
        test_duration_classification,
        test_ytd_chain_is_differenced,
        test_q4_is_derived_when_absent,
        test_discrete_quarters_preferred_over_derived,
        test_instant_facts_are_never_differenced,
        test_missing_middle_quarter,
        test_micron_real_shape,
        test_restated_fact_in_same_slot_uses_latest_filed,
        test_fiscal_year_not_calendar_year,
        test_non_negative_default_preserves_prior_behaviour,
        test_negative_derived_quarter_dropped_when_non_negative,
        test_non_negative_never_drops_a_reported_or_instant_value,
        test_implausible_quarters_reports_what_was_dropped,
    ]:
        fn()
    print("\nall quarterization tests passed")


# ---------------------------------------------------------------------------
# TRAP 5 — never difference an average. (finlake 0.4.0)
# ---------------------------------------------------------------------------
def _ytd(tag, start, end, val, filed, accn):
    return {"tag": tag, "period_start": start, "period_end": end, "val": val,
            "fy": None, "fp": None, "form": "10-Q", "accn": accn,
            "filed": filed, "unit": "shares"}


def test_averages_are_never_differenced():
    """The Apple bug, minimised.

    Weighted-average diluted shares is a duration fact, so every flow-vs-instant
    check passes it straight into the year-to-date differencing path. But it is
    an average OVER the period, not an accumulation across it, so `FY - 9M`
    subtracts two nearly identical numbers.

    Here the share count drifts gently down (1000 -> 970) as a company buys
    back stock. The YTD averages are the running means. Differencing them would
    give Q4 = 985 - 990 = -5 shares; on real Apple data it produced NEGATIVE 55
    MILLION shares, which then dragged the annual figure 25% below truth and
    made every per-share number derived from it about a third too high.
    """
    from finlake.quarterize import quarterize_average

    # True quarterly averages: 1000, 990, 980, 970  (mean of first k reported
    # as the year-to-date figure, which is how filers tag this).
    rows = [
        _ytd("S", "2023-01-01", "2023-03-31", 1000.0, "2023-04-30", "a1"),
        _ytd("S", "2023-01-01", "2023-06-30", 995.0, "2023-07-31", "a2"),
        _ytd("S", "2023-01-01", "2023-09-30", 990.0, "2023-10-31", "a3"),
        _ytd("S", "2023-01-01", "2023-12-31", 985.0, "2024-01-31", "a4"),
    ]
    qs = {q.period_end: q.value for q in quarterize_average(rows)}

    assert len(qs) == 4, f"expected 4 quarters, got {sorted(qs)}"
    for end, expected in [("2023-03-31", 1000), ("2023-06-30", 990),
                          ("2023-09-30", 980), ("2023-12-31", 970)]:
        assert abs(qs[end] - expected) < 1.0, (
            f"{end}: got {qs[end]:.1f}, expected ~{expected}. The averaging "
            f"identity Q_k = k*YTD_k - (k-1)*YTD_(k-1) did not recover the "
            f"discrete quarter.")

    assert all(v > 0 for v in qs.values()), (
        f"an average was differenced and went negative: {qs}")


def test_average_quarterization_recovers_the_unreported_fourth_quarter():
    """There is no Q4 10-Q, so the fiscal fourth quarter's average share count
    is never filed directly — only the full-year average is. It has to be
    recovered from the identity, or every fiscal Q4 in the database is a gap.
    """
    from finlake.quarterize import quarterize_average

    rows = [
        _ytd("S", "2023-01-01", "2023-03-31", 100.0, "2023-04-30", "b1"),
        _ytd("S", "2023-01-01", "2023-06-30", 100.0, "2023-07-31", "b2"),
        _ytd("S", "2023-01-01", "2023-09-30", 100.0, "2023-10-31", "b3"),
        # Full year averages 95 => the Q4 average must have been 80.
        _ytd("S", "2023-01-01", "2023-12-31", 95.0, "2024-01-31", "b4"),
    ]
    qs = {q.period_end: q.value for q in quarterize_average(rows)}
    assert abs(qs["2023-12-31"] - 80.0) < 0.5, (
        f"Q4 came out as {qs['2023-12-31']}, expected 80 "
        f"(4*95 - 3*100 = 80)")


def test_passthrough_never_invents_a_quarter():
    """A reported ratio can't be reconstructed by any arithmetic across
    periods, so it is taken as filed. A quarter-length fact is preferred over
    a year-to-date one at the same period end; a YTD figure is kept only when
    it's all there is."""
    from finlake.quarterize import quarterize_passthrough

    rows = [
        # Same period end, two spans: the discrete quarter must win.
        {"tag": "R", "period_start": "2023-01-01", "period_end": "2023-12-31",
         "val": 0.21, "fy": None, "fp": None, "form": "10-K", "accn": "c1",
         "filed": "2024-01-31", "unit": "pure"},
        {"tag": "R", "period_start": "2023-10-01", "period_end": "2023-12-31",
         "val": 0.18, "fy": None, "fp": None, "form": "10-K", "accn": "c2",
         "filed": "2024-01-31", "unit": "pure"},
    ]
    qs = {q.period_end: q.value for q in quarterize_passthrough(rows)}
    assert qs["2023-12-31"] == 0.18, (
        "the year-to-date rate was preferred over the discrete quarter")
    assert all(not q.derived for q in quarterize_passthrough(rows)), (
        "passthrough must never mark a value as derived — it does no arithmetic")
