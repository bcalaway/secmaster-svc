"""Treasury futures' baskets and conversion factors (app/baskets.py) and their storage (app/futures_load.py)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import baskets as b
from app import db, futures_seed
from app.models import FuturesContract, FuturesDeliverable, Instrument, SecurityTerms
from tests.test_futures_load import NOW, run

DEC26 = date(2026, 12, 1)


def rule(root: str) -> b.BasketRule:
    return next(p.basket for p in futures_seed.load().products if p.product.root == root)


def sec(maturity: date, coupon="0.04", issue: date = date(2025, 1, 31), kind="note", sec_id=1, call=None):
    return b.Security(sec_id, f"91282C{sec_id:03d}", kind, Decimal(coupon), 2, issue, maturity, call)


def price_at_six(coupon: Decimal, months: int) -> Decimal:
    """An independent check: the clean price per 1 at a 6% semi-annual yield, cash flow by cash flow."""
    rest = months % 6  # months to the next coupon, if not on a coupon date
    count = months // 6 + (1 if rest else 0)
    times = [rest / 6 + k for k in range(count)] if rest else [k + 1 for k in range(count)]
    c = float(coupon)
    dirty = sum(c / 2 / 1.03 ** t for t in times) + 1 / 1.03 ** times[-1]
    accrued = c / 2 * (6 - rest) / 6 if rest else 0
    return Decimal(f"{dirty - accrued:.4f}")


@pytest.mark.parametrize("months", [12, 18, 60, 96, 120, 240, 300])
def test_a_six_percent_coupon_on_a_half_year_is_par(months):
    assert b.conversion_factor(Decimal("0.06"), months) == Decimal("1.0000")


@pytest.mark.parametrize(("coupon", "months"), [("0.04125", 81), ("0.0425", 90), ("0.045", 117), ("0.02375", 21),
                                                ("0.05", 297), ("0.0175", 360), ("0.0625", 87), ("0.03875", 50)])
def test_the_factor_is_the_price_at_six_percent(coupon, months):
    assert b.conversion_factor(Decimal(coupon), months) == price_at_six(Decimal(coupon), months)


def test_tu_window_is_inclusive_at_both_ends():
    r = rule("TU")
    # 1y9m and 2y from Dec 1, 2026, in whole months (rounded down): Sep 1, 2028 in, a day before out.
    assert b.eligible(r, sec(date(2028, 9, 1)), DEC26, date(2027, 1, 5)) == 21
    assert b.eligible(r, sec(date(2028, 8, 31)), DEC26, date(2027, 1, 5)) is None
    assert b.eligible(r, sec(date(2028, 11, 30), issue=date(2026, 11, 30)), DEC26, date(2027, 1, 5)) == 23
    assert b.eligible(r, sec(date(2028, 12, 1)), DEC26, date(2027, 1, 5)) == 24
    assert b.eligible(r, sec(date(2029, 1, 1)), DEC26, date(2027, 1, 5)) is None  # 2 years 1 month


def test_ty_excludes_eight_years_and_original_terms_over_ten():
    r = rule("TY")
    assert b.eligible(r, sec(date(2034, 11, 30), issue=date(2024, 11, 30)), DEC26, date(2026, 12, 31)) == 93
    assert b.eligible(r, sec(date(2034, 12, 1), issue=date(2024, 12, 1)), DEC26, date(2026, 12, 31)) is None
    # A 30-year bond with 7 years left is in the bond basket's past, not the 10-year's.
    assert b.eligible(r, sec(date(2033, 11, 15), issue=date(2003, 11, 15), kind="bond"), DEC26, date(2026, 12, 31)) is None


def test_only_fixed_coupon_notes_and_bonds_issued_by_the_last_delivery_day():
    r = rule("FV")
    good = sec(date(2031, 9, 30), issue=date(2026, 9, 30))
    assert b.eligible(r, good, DEC26, date(2027, 1, 6)) == 57
    for bad in (sec(date(2031, 9, 30), kind="tips"), sec(date(2031, 9, 30), kind="frn"),
                sec(date(2031, 11, 30), issue=date(2027, 1, 7))):
        assert b.eligible(r, bad, DEC26, date(2027, 1, 6)) is None


def test_a_callable_bond_counts_to_its_first_call():
    r = rule("US")
    callable_ = sec(date(2044, 11, 15), kind="bond", issue=date(2014, 11, 15), call=date(2039, 11, 15))
    assert b.eligible(r, callable_, DEC26, date(2026, 12, 31)) is None  # 12y11m to its call: under 15 years
    later = sec(date(2051, 11, 15), kind="bond", issue=date(2021, 11, 15), call=date(2046, 11, 15))
    assert b.eligible(r, later, DEC26, date(2026, 12, 31)) == 237  # 19y11m to the call, down to a quarter


def test_basket_lists_by_maturity_and_refuses_the_eight_percent_years():
    r = rule("TY")
    out = b.basket(r, [sec(date(2034, 8, 15), sec_id=2, issue=date(2024, 8, 15)),
                       sec(date(2033, 8, 15), sec_id=3, issue=date(2023, 8, 15))], DEC26, date(2026, 12, 31))
    assert [d.sec_id for d in out] == [3, 2] and out[0].remaining_months == 78
    with pytest.raises(b.BasketError):
        b.basket(r, [], date(1999, 12, 1), date(1999, 12, 31))


def test_seed_rules_parse_and_say_what_they_are():
    assert rule("TY").text == "remaining >= 6y6m and < 8y; original term <= 10y; factor rounded to quarters"
    with pytest.raises(b.BasketError):
        b.parse_rule({"remaining_min_months": 20, "remaining_max_months": 10}, "x")


def _add_note(s, sec_id: int, cusip: str, coupon: str, issue: date, maturity: date):
    s.add(Instrument(sec_id=sec_id, type="ust_note", currency="USD", country="US", calendar="SIFMA-US"))
    s.add(SecurityTerms(sec_id=sec_id, cusip=cusip, security_type="note", cmb=False, issue_date=issue,
                        maturity_date=maturity, coupon_rate=Decimal(coupon), coupon_frequency=2, day_count="ACT/ACT",
                        redemption=Decimal(100), settlement_days=1, calendar="SIFMA-US", provenance={}, checks=[],
                        recorded_at=NOW))


def test_the_load_stores_each_listed_contracts_basket_once(migrated_db):
    with db.session() as s:
        _add_note(s, 900001, "91282CLA1", "0.04125", date(2024, 11, 30), date(2031, 11, 30))  # 5-year: FV
        _add_note(s, 900002, "91282CLB2", "0.0425", date(2024, 11, 15), date(2034, 11, 15))  # 10-year: TY
        s.commit()
    first = run()
    assert first["deliverables_added"] >= 2
    second = run()
    assert second["deliverables_added"] == second["deliverables_changed"] == second["deliverables_removed"] == 0
    with db.session() as s:
        rows = s.execute(select(FuturesDeliverable, FuturesContract).join(
            FuturesContract, (FuturesContract.sec_id == FuturesDeliverable.contract_sec_id)
            & FuturesContract.superseded_at.is_(None)).where(FuturesDeliverable.security_sec_id == 900002)).all()
        months = {c.contract_month: d for d, c in rows}
        assert months[DEC26].conversion_factor == b.conversion_factor(Decimal("0.0425"), 93)
        assert months[DEC26].valid_from == date(2024, 11, 15)
        assert all(d.superseded_at is None for d, _ in rows)


def test_a_changed_term_supersedes_the_factor(migrated_db):
    with db.session() as s:
        _add_note(s, 900003, "91282CLC3", "0.0425", date(2024, 11, 15), date(2034, 11, 15))
        s.commit()
    run()
    with db.session() as s:
        t = s.scalars(select(SecurityTerms).where(SecurityTerms.sec_id == 900003)).one()
        t.coupon_rate = Decimal("0.04375")
        s.commit()
    out = run(now=datetime(2026, 10, 8, 13, tzinfo=UTC))
    assert out["deliverables_changed"] >= 1 and out["deliverables_added"] == 0


# Spot checks from CME's conversion factor table of 2026-10-08 (TCF.xlsx, downloaded by hand for this check;
# the whole table matched in the sandbox, 2,194 factors across the 8 products, every basket and every factor,
# but it isn't committed: this repo is public and the table is CME's). Terms as Treasury publishes them.
CME_2026_10_08 = [
    ("TU", "91282CQV6", "0.04125", date(2026, 6, 15), date(2029, 6, 15), date(2027, 6, 1), "0.9652"),
    ("3Y", "91282CPA3", "0.03625", date(2025, 9, 30), date(2030, 9, 30), date(2027, 9, 1), "0.9357"),
    ("FV", "91282CQX2", "0.04125", date(2026, 6, 30), date(2031, 6, 30), DEC26, "0.9270"),
    ("TY", "91282CLF6", "0.03875", date(2024, 8, 15), date(2034, 8, 15), DEC26, "0.8732"),
    ("UXY", "91282CRF0", "0.04625", date(2026, 8, 17), date(2036, 8, 15), DEC26, "0.9015"),
    ("TWEA", "912810RU4", "0.02875", date(2016, 11, 15), date(2046, 11, 15), date(2027, 3, 1), "0.6436"),
    ("US", "912810RV2", "0.03", date(2017, 2, 15), date(2047, 2, 15), DEC26, "0.6533"),
    ("WN", "912810UA4", "0.04625", date(2024, 5, 15), date(2054, 5, 15), DEC26, "0.8165"),
]


@pytest.mark.parametrize(("root", "cusip", "coupon", "issue", "maturity", "month", "factor"), CME_2026_10_08)
def test_cmes_table(root, cusip, coupon, issue, maturity, month, factor):
    s = b.Security(1, cusip, "note", Decimal(coupon), 2, issue, maturity)
    got = b.basket(rule(root), [s], month, b.add_months(month, 1))
    assert [(d.cusip, d.conversion_factor) for d in got] == [(cusip, Decimal(factor))]


def test_the_20_year_takes_30_year_bonds_issued_before_august_2018_only():
    r = rule("TWEA")
    sc3 = b.Security(1, "912810SC3", "bond", Decimal("0.03125"), 2, date(2018, 5, 15), date(2048, 5, 15))
    sd1 = b.Security(2, "912810SD1", "bond", Decimal("0.03"), 2, date(2018, 8, 15), date(2048, 8, 15))
    dec28 = date(2028, 12, 1)
    assert [d.cusip for d in b.basket(r, [sc3, sd1], dec28, date(2028, 12, 29))] == ["912810SC3"]


def test_the_two_year_rounds_the_remaining_term_down_before_the_window():
    # 2 years and 30 days from Dec 1, 2026 rounds down to 2 years: in, as in CME's table (91282CJR3).
    assert b.eligible(rule("TU"), sec(date(2028, 12, 31), issue=date(2023, 12, 31)), DEC26, date(2027, 1, 5)) == 24

