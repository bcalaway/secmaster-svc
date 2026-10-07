"""Typing TreasuryDirect's records into auctions and terms (app/treasuries.py), on real captures."""

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import treasuries as tr

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _securities(name: str) -> dict[str, list[tr.Auction]]:
    by = defaultdict(list)
    for r in json.loads((FIXTURES / name).read_text()):
        a = tr.type_auction(f"{r['cusip']}/{r['issueDate'][:10]}", r)
        by[a.cusip].append(a)
    return by


FEB = _securities("td_securities_2026_02_capture1319.json")
OCT = _securities("td_securities_2026_10_capture1261.json")
Y1980 = _securities("td_securities_1980_02_capture1273.json")


def test_term_iso():
    assert tr.term_iso("10-Year") == "P10Y" and tr.term_iso("29-Year 10-Month") == "P29Y10M"
    assert tr.term_iso("13-Week") == "P13W" and tr.term_iso("30-Year 0-Month") == "P30Y"
    assert tr.term_iso(None) is None
    with pytest.raises(tr.Untypable):
        tr.term_iso("ten years")


def test_identifiers():
    assert tr.isin("037833100") == "US0378331005"  # Apple, a known ISIN
    assert tr.cusip_ok("91282CQC8") and tr.cusip_ok("912810UW6") and not tr.cusip_ok("91282CQC9")
    assert all(tr.cusip_ok(c) for c in [*FEB, *OCT, *Y1980])
    assert all(tr.isin(c).startswith("US" + c) and len(tr.isin(c)) == 12 for c in FEB)


def test_seven_year_note():
    t = tr.build_terms(FEB["91282CQC8"])
    v = t.values
    assert tr.short_name(v) == "UST-3.75-2033-02-28" and v["security_type"] == "note" and v["term"] == "7-Year"
    assert v["coupon_rate"] == Decimal("0.0375") and v["coupon_frequency"] == 2 and v["day_count"] == "ACT/ACT-ICMA"
    assert (v["announcement_date"], v["auction_date"], v["issue_date"]) == (
        date(2026, 2, 19), date(2026, 2, 26), date(2026, 3, 2))
    # Accrues from Feb 28 (the dated date), pays on the last day of Aug and Feb: the end-of-month rule.
    assert v["dated_date"] == date(2026, 2, 28) and v["end_of_month"] is True
    assert v["first_coupon_date"] == date(2026, 8, 31) and v["first_period_type"] == "Normal"
    assert v["penultimate_coupon_date"] == date(2032, 8, 31)
    assert v["corpus_cusip"] == "912821TY1" and v["strippable"] is True
    assert t.provenance["coupon_rate"] == "published: TD-SECURITIES 91282CQC8/2026-03-02 interestRate"
    assert t.provenance["penultimate_coupon_date"].startswith("derived:")
    assert t.checks == []


def test_two_year_note_maturing_on_leap_day():
    v = tr.build_terms(next(a for a in FEB.values() if a[0].maturity_date == date(2028, 2, 29))).values
    assert v["first_coupon_date"] == date(2026, 8, 31) and v["penultimate_coupon_date"] == date(2027, 8, 31)


def test_tips():
    t = tr.build_terms(FEB["912810US5"])
    v = t.values
    assert tr.short_name(v) == "UST-TII-2.375-2056-02-15" and v["security_type"] == "tips"
    assert v["tips_base_cpi"] == Decimal("324.088000") and v["cpi_base_period"] == "1982-1984=100"
    assert t.checks == []


def test_frn_reopening_without_its_original():
    t = tr.build_terms(FEB["91282CPX3"])
    v = t.values
    assert tr.short_name(v) == "UST-FRN-2028-01-31" and v["security_type"] == "frn"
    assert v["frn_spread"] == Decimal("0.00099") and v["frn_index"] == "13-week bill high rate"
    assert v["coupon_rate"] is None and v["coupon_frequency"] == 4 and v["day_count"] == "ACT/360"
    # The original auction (2026-01) isn't in this month: its issue date comes from the reopening.
    assert v["issue_date"] == date(2026, 2, 2) and v["auction_date"] is None and v["term"] == "2-Year"
    assert v["penultimate_coupon_date"] == date(2027, 10, 31)
    assert [tr.check_code(c) for c in t.checks] == ["original-not-loaded"]
    assert [a.results["frn_index_rate"] for a in FEB["91282CPX3"]] == [Decimal("0.0359")]


def test_bills():
    reopened = [a[0] for a in FEB.values() if a[0].security_type == "bill" and a[0].reopening]
    assert reopened and all(a.term in ("4-Week", "6-Week", "8-Week", "13-Week", "26-Week") for a in reopened)
    one = reopened[0]
    v = tr.build_terms([one]).values
    assert v["coupon_frequency"] == 0 and v["day_count"] == "ACT/360" and v["dated_date"] is None
    assert v["term"] == one.original_term  # the program it was first auctioned as: 17-, 26- or 52-week
    assert tr.short_name(v) == f"UST-B-{one.maturity_date.isoformat()}"
    assert one.results["high_discount_rate"] is not None and one.results["high_investment_rate"] is not None


def test_1980():
    bond = tr.build_terms(Y1980["912810CM8"]).values
    assert tr.short_name(bond) == "UST-11.75-2010-02-15"
    assert bond["callable"] is True and bond["call_date"] == date(2005, 2, 15) and bond["called_date"] == date(2005, 2, 15)
    note = tr.build_terms(Y1980["912827KM3"])
    assert note.values["original_term"] == "5-Year 2-Month" and note.values["term"] == "5-Year"
    assert note.values["first_period_type"] == "Long" and note.checks == []
    # 13-week bills reopening 26-week bills auctioned before February 1980: no original term yet.
    unknown = [c for c, a in Y1980.items() if tr.build_terms(a).values["term"] is None]
    assert len(unknown) == 4


def test_coupon_labels():
    assert [tr.coupon_label(Decimal(x)) for x in ("0.0425", "0.05125", "0.04", "0.00125", "0.14375")] == [
        "4.25", "5.125", "4.00", "0.125", "14.375"]


def test_a_reopening_and_its_original_together():
    orig = next(a for a in FEB["91282CQC8"])
    reopen = tr.Auction(**{**orig.__dict__, "source_key": "91282CQC8/2026-04-30", "reopening": True,
                           "issue_date": date(2026, 4, 30), "security_term": "6-Year 10-Month",
                           "results": {}})
    t = tr.build_terms([reopen, orig])
    assert t.values["issue_date"] == date(2026, 3, 2) and t.values["original_term"] == "7-Year" and t.checks == []
    other = tr.Auction(**{**reopen.__dict__, "coupon_rate": Decimal("0.04")})
    assert any(c.startswith("auctions-disagree: on coupon_rate") for c in tr.build_terms([orig, other]).checks)


def test_untypable():
    with pytest.raises(tr.Untypable, match="type"):
        tr.type_auction("X/2026-01-01", {"cusip": "912810UW6", "type": "Swap", "maturityDate": "2030-01-01"})
    with pytest.raises(tr.Untypable, match="number"):
        tr.type_auction("X/2026-01-01", {"cusip": "912810UW6", "type": "Bond", "maturityDate": "2030-01-01",
                                         "interestRate": "n/a"})
    with pytest.raises(tr.Untypable, match="maturityDate"):
        tr.type_auction("X/2026-01-01", {"cusip": "912810UW6", "type": "Bond"})


def test_an_annual_foreign_targeted_note():
    """1984-1986's foreign-targeted notes paid once a year (912827TG7, the 8.875% of 1996-02-15)."""
    base = next(a for a in Y1980["912827KM3"])
    ftn = tr.Auction(**{**base.__dict__, "source_key": "912827TG7/1986-02-15", "cusip": "912827TG7",
                        "frequency": "Annual", "dated_date": date(1986, 2, 15), "maturity_date": date(1996, 2, 15),
                        "first_coupon_date": date(1987, 2, 15), "first_period_type": "Normal",
                        "coupon_rate": Decimal("0.08875"), "issue_date": date(1986, 2, 18)})
    t = tr.build_terms([ftn])
    assert t.values["coupon_frequency"] == 1 and t.checks == []
    assert t.values["penultimate_coupon_date"] == date(1995, 2, 15)
