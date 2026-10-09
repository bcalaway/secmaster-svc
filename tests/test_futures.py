"""The contract generator (app/futures.py) and the product seed (app/futures_seed.py).

Dates are checked by hand against the rules on a few known contracts, with a small calendar of
2026's holidays (US: New Year, MLK, Presidents', Memorial, Juneteenth, July 3, Labor Day,
Thanksgiving, Christmas; TARGET: New Year, Good Friday, Easter Monday, May 1, Christmas and
St Stephen's). The real calendars come from calendar-svc.
"""

from datetime import date

import pytest

from app import futures as f
from app import futures_seed

US_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19),
           date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)}
TARGET_2026 = {date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1), date(2026, 12, 25),
               date(2026, 12, 26), date(2027, 1, 1)}
TODAY = date(2026, 10, 8)


def cals() -> f.Calendars:
    us = frozenset(US_2026)
    names = ("CME-IR", "CME-FX", "FED", "TARGET", "JP", "GB", "AU", "CA", "CH", "MX", "NZ")
    closed = {n: us for n in names} | {"TARGET": frozenset(TARGET_2026)}
    return f.Calendars(closed, dict.fromkeys(names, (1990, 2100)))


@pytest.fixture(scope="module")
def seed():
    return futures_seed.load()


def product(seed, root) -> f.Product:
    return next(p.product for p in seed.products if p.product.root == root)


def contract(seed, root, year, month, today=TODAY):
    got = f.generate(product(seed, root), cals(), today)
    return next(d for d in got.contracts if d.contract.month == date(year, month, 1))


def test_seed_parses_twenty_products(seed):
    roots = [p.product.root for p in seed.products]
    assert len(roots) == 20 and len(set(roots)) == 20
    assert {"TU", "3Y", "FV", "TY", "UXY", "TWEA", "US", "WN", "FF", "SER", "SFR", "TZR",
            "EC", "JY", "BP", "AD", "CD", "SF", "PE", "NV"} == set(roots)
    for p in seed.products:
        assert p.specs and all(s.source.startswith("https://www.cmegroup.com/") for s in p.specs)
        assert set(p.rule_sources) == set(p.product.rules)


def test_ten_year_dates(seed):
    d = contract(seed, "TY", 2026, 12)
    assert d.contract.short_name == "TYZ26" and d.contract.cme_symbol == "ZNZ6"
    assert d.dates["last_trade_date"] == date(2026, 12, 21)  # 7 business days before Dec 31 (Christmas closed)
    assert d.dates["first_intention_date"] == date(2026, 11, 27)  # 2 business days before Dec 1
    assert d.dates["first_notice_date"] == date(2026, 11, 30)
    assert d.dates["first_delivery_date"] == date(2026, 12, 1)
    assert d.dates["last_delivery_date"] == date(2026, 12, 31)
    assert d.status == "listed" and d.listed_today
    assert "last_bd_minus:7" in d.rules["last_trade_date"] and "CME-IR" in d.rules["last_trade_date"]


def test_two_year_delivers_three_days_after_month_end(seed):
    d = contract(seed, "TU", 2026, 12)
    assert d.dates["last_trade_date"] == date(2026, 12, 31)
    assert d.dates["last_delivery_date"] == date(2027, 1, 6)  # Jan 1 closed: Jan 4, 5, 6


def test_treasury_listing_matches_cme_quotes(seed):
    # CME's ZT quotes page on 2026-10-08 listed Dec 2026, Mar 2027 and Jun 2027.
    got = f.generate(product(seed, "TU"), cals(), TODAY)
    assert [d.contract.short_name for d in got.contracts if d.listed_today] == ["TUZ26", "TUH27", "TUM27"]


def test_first_trade_date_from_the_cycle(seed):
    # ZNM7 became the third listed quarterly when ZNU6 expired (2026-09-21), so it's listed from the 22nd.
    assert contract(seed, "TY", 2026, 9).dates["last_trade_date"] == date(2026, 9, 21)
    assert contract(seed, "TY", 2027, 6).dates["first_trade_date"] == date(2026, 9, 22)
    old = contract(seed, "TY", 2001, 3)
    assert old.status == "expired" and old.dates["first_trade_date"] is None
    assert old.rules["first_trade_date"].startswith("unknown")


def test_delivery_status_and_roll(seed):
    on = date(2026, 12, 2)
    d = contract(seed, "TY", 2026, 12, today=on)
    assert d.status == "delivery"
    got = f.generate(product(seed, "TY"), cals(), on)
    front = [g for g in got.generics if g.alias == "TY1" and g.valid_from <= on <= (g.valid_to or on)]
    assert [g.contract.short_name for g in front] == ["TYH27"]  # rolled on Dec's first intention day


def test_generics_roll_at_first_intention_day(seed):
    got = f.generate(product(seed, "TY"), cals(), TODAY)
    by = {(g.alias, g.valid_from): g for g in got.generics}
    now = [g for g in got.generics if g.valid_from <= TODAY <= g.valid_to]
    assert {g.alias: g.contract.short_name for g in now} == {"TY1": "TYZ26", "TY2": "TYH27", "TY3": "TYM27"}
    tyz = next(g for g in now if g.alias == "TY1")
    assert tyz.valid_to == date(2026, 11, 26)  # the day before first intention day
    assert ("TY1", date(1990, 1, 1)) in by  # from the history start


def test_fed_funds_monthly(seed):
    d = contract(seed, "FF", 2026, 10)
    assert d.dates["last_trade_date"] == date(2026, 10, 30)
    assert d.dates["final_settlement_date"] == date(2026, 11, 2)
    assert (d.dates["reference_start"], d.dates["reference_end"]) == (date(2026, 10, 1), date(2026, 11, 1))
    got = f.generate(product(seed, "FF"), cals(), TODAY)
    listed = [x for x in got.contracts if x.listed_today]
    assert len(listed) == 60 and listed[0].contract.short_name == "FFV26" and listed[-1].contract.short_name == "FFU31"


def test_three_month_sofr(seed):
    d = contract(seed, "SFR", 2026, 12)
    assert d.dates["last_trade_date"] == date(2026, 12, 15)
    assert (d.dates["reference_start"], d.dates["reference_end"]) == (date(2026, 9, 16), date(2026, 12, 16))
    got = f.generate(product(seed, "SFR"), cals(), TODAY)
    listed = [x for x in got.contracts if x.listed_today]
    assert sum(x.contract.quarterly for x in listed) == 39 and sum(not x.contract.quarterly for x in listed) == 6
    # Serial months are kept only while listed; quarterly history goes back to the launch.
    assert all(x.contract.quarterly for x in got.contracts if not x.listed_today)
    assert got.contracts[0].contract.short_name == "SFRM18"


def test_t_bill_last_trade_is_the_monday(seed):
    assert contract(seed, "TZR", 2026, 12).dates["last_trade_date"] == date(2026, 12, 14)


def test_fx_dates(seed):
    d = contract(seed, "EC", 2026, 12)
    assert d.dates["last_trade_date"] == date(2026, 12, 14)  # 2 business days before Dec 16
    assert d.dates["settlement_date"] == date(2026, 12, 16)
    assert contract(seed, "CD", 2026, 12).dates["last_trade_date"] == date(2026, 12, 15)  # 1 business day


def test_fx_settlement_moves_past_a_delivery_holiday():
    p = f.Product("XX", "XX", "fx", ("CME-FX", "FED"), ("TARGET", "FED"),
                  {"last_trade_date": "third_wed_minus:2", "settlement_date": "third_wed_next_good"},
                  f.Listing(quarterly=4), None, 2)
    c = cals()
    c.closed["TARGET"] = c.closed["TARGET"] | {date(2026, 12, 16)}
    d = f.dates_for(f.Contract(p, date(2026, 12, 1)), c)
    assert d.dates["settlement_date"] == date(2026, 12, 17)


def test_no_history_means_only_todays_listings(seed):
    got = f.generate(product(seed, "PE"), cals(), TODAY)
    assert got.contracts and all(d.listed_today for d in got.contracts)


def test_calendar_coverage_is_checked(seed):
    c = cals()
    c.years["CME-IR"] = (2000, 2100)
    with pytest.raises(f.CalendarError):
        f.generate(product(seed, "TY"), c, TODAY)


@pytest.mark.parametrize("bad, why", [
    ('read_on = 2026-10-08\n', "no products"),
    ('read_on = 2026-10-08\n[[products]]\nroot = "t"\n', "bad root"),
])
def test_seed_rejects(bad, why):
    with pytest.raises(futures_seed.FuturesSeedError, match=why):
        futures_seed.parse(bad.encode())


def test_seed_rejects_unknown_rule():
    body = futures_seed.PATH.read_text().replace('"last_bd_minus:7"', '"last_bd_minus:x"', 1)
    with pytest.raises(futures_seed.FuturesSeedError, match="whole number"):
        futures_seed.parse(body.encode())
