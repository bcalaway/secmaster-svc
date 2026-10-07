"""TIPS reference CPI and index ratios (app/tips.py) and the load's check against TreasuryDirect."""

from datetime import date
from decimal import Decimal

import pytest

from app import db, load, securities, tips
from tests.test_load import NOW, TODAY, FakeMktData, _records

# November and December 2025 CPI-U as TreasuryDirect's February 2026 TIPS imply them: its reference CPI on
# the dated date (Feb 15, halfway) is 324.088 and on the issue date (Feb 27) 324.05886.
NOV, DEC = Decimal("324.122"), Decimal("324.054")


def test_the_rule_on_treasurydirects_february_2026_tips():
    months = {date(2025, 11, 1): tips.MonthCpi(NOV, "published"), date(2025, 12, 1): tips.MonthCpi(DEC, "published")}
    assert tips.ref_cpi(date(2026, 2, 1), months).value == NOV  # the 1st: CPI(M-3) exactly
    assert tips.ref_cpi(date(2026, 2, 15), months).value == Decimal("324.08800")  # the dated date: the base
    issue = tips.ref_cpi(date(2026, 2, 27), months).value
    assert issue == Decimal("324.05886")  # five decimals; six would give 324.058857, not what Treasury prints
    assert tips.index_ratio(issue, Decimal("324.088")) == Decimal("0.99991")
    assert tips.ref_cpi(date(2026, 3, 1), months) is None  # needs January 2026


def test_fallback_for_a_month_never_published():
    published = {}
    m = date(2024, 9, 1)
    # September 2024 to November 2025, then October 2025 taken out.
    for i, v in enumerate(["315.301", "315.664", "315.493", "315.605", "317.671", "319.082", "319.799", "320.795",
                           "321.465", "322.561", "323.048", "323.976", "324.800", "0", "324.122"]):
        published[tips._month(m, i)] = Decimal(v)
    del published[date(2025, 10, 1)]
    filled = tips.fill_months(published)
    oct25 = filled[date(2025, 10, 1)]
    # CPI(Sep 2025) x (CPI(Sep 2025) / CPI(Sep 2024)) ^ (1/12)
    want = Decimal("324.800") * (Decimal("324.800") / Decimal("315.301")) ** (Decimal(1) / 12)
    assert oct25.method == "fallback" and oct25.value == round(want, 3)
    assert tips.ref_cpi(date(2026, 1, 10), filled).method == "fallback"
    with pytest.raises(ValueError, match="can't be filled"):
        tips.fill_months({date(2025, 1, 1): Decimal(1), date(2025, 3, 1): Decimal(1)})


def test_series_span():
    months = {tips._month(date(2025, 11, 1), i): tips.MonthCpi(Decimal(300 + i), "published") for i in range(3)}
    series = tips.ref_cpi_series(months)
    # CPI through January: the last day whose M-2 month is January is March 31.
    assert series[0].day == date(2026, 2, 1) and series[-1].day == date(2026, 3, 31)


def test_load_checks_tips_against_treasurydirect(migrated_db):
    mkt = FakeMktData()
    mkt.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    mkt.put_cpi("2025", [("2025-11", "324.122"), ("2025-12", "324.054")])
    with db.session() as s:
        out = load.run(s, mkt, NOW, TODAY)
    cpi = out["tips_cpi"]
    # The 30-year TIPS: ref CPI on the issue date, index ratio on the issue date, ref CPI on the dated date.
    assert (cpi["compared"], cpi["matched"], cpi["mismatched"]) == (3, 3, 0)
    assert out["reference_cpi_days"] == 28 and out["reference_cpi_through"] == "2026-02-28"  # February only
    with db.session() as s:
        assert securities.reference_cpi(s, date(2026, 2, 27))["ref_cpi"].startswith("324.05886")
        t = securities.get(s, name="UST-TII-2.375-2056-02-15", as_of=date(2026, 2, 27))
        assert Decimal(t["index_ratio"]["index_ratio"]) == Decimal("0.99991")
    # A revised month moves the reference CPI, and the check catches the difference.
    mkt.put_cpi("2025", [("2025-11", "324.122"), ("2025-12", "324.060")])
    with db.session() as s:
        again = load.run(s, mkt, NOW, TODAY)
    assert again["cpi_months_changed"] == 1 and again["tips_cpi"]["mismatched"] == 3
    assert again["tips_cpi"]["mismatches"][0]["treasurydirect"].startswith("324.05886")


def test_a_long_gap_is_history_not_loaded_not_a_chain_of_fallbacks():
    # BLS 1996-2015 and 2026 loaded, 2016-2025 not yet (the hub on 2026-10-07): no fallback across the gap.
    published = {tips._month(date(2014, 1, 1), i): Decimal("236.000") for i in range(24)}
    published |= {tips._month(date(2026, 1, 1), i): Decimal("325.000") for i in range(8)}
    filled = tips.fill_months(published)
    assert all(v.method == "published" for v in filled.values()) and date(2016, 1, 1) not in filled
    assert tips.ref_cpi(date(2017, 2, 28), filled) is None  # needs Nov and Dec 2016
    assert tips.ref_cpi(date(2016, 2, 29), filled).value == Decimal("236.00000")  # Nov and Dec 2015: loaded
    assert tips.ref_cpi(date(2016, 3, 1), filled) is None  # Jan 2016: not loaded


def test_known_exceptions_hold_only_while_treasury_prints_the_same_figure():
    assert load._known_exception("9128275W8/2000-07-17", "ref CPI on issue date", "171.251610")
    assert load._known_exception("912810FH6/2000-10-16", "index ratio on issue date", "1.050220")
    assert not load._known_exception("9128275W8/2000-07-17", "ref CPI on issue date", "171.40323")  # corrected
    assert not load._known_exception("9128275W8/2000-07-17", "ref CPI on dated date", "171.251610")
