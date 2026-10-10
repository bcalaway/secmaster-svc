"""Lookups (app/securities.py): what quote-svc and mkt-api ask."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db, securities, seed

SEEDS = Path(__file__).resolve().parent.parent / "seeds"


@pytest.fixture
def seeded(migrated_db):
    with db.session() as s:
        seed.apply_all(s, SEEDS)
    return migrated_db


def test_get_by_name_alias_or_sec_id(seeded):
    with db.session() as s:
        ten = securities.get(s, name="ust-10y-cmt")
        six = securities.get(s, name="UST-6W-CMT")
        again = securities.get(s, sec_id=ten["sec_id"])
    assert ten["short_name"] == "UST-10Y-CMT" and ten["tenor"] == "P10Y" and ten["calendar"] == "SIFMA-US"
    assert {(i["scheme"], i["value"]) for i in ten["identifiers"]} == {
        ("UST-PAR", "BC_10YEAR"), ("H15-TCM", "RIFLGFCY10_N.B"), ("FRED", "DGS10")}
    assert six["short_name"] == "UST-1.5M-CMT" and six["aliases"] == ["UST-6W-CMT"]
    assert again == ten
    with db.session() as s, pytest.raises(securities.UnknownInstrument):
        securities.get(s, name="NOPE")


def test_list_is_a_curve_in_tenor_order(seeded):
    with db.session() as s:
        names = [i["short_name"] for i in securities.list_instruments(s, type="cmt_yield", curve="ust")]
    assert names == [
        "UST-1M-CMT", "UST-1.5M-CMT", "UST-2M-CMT", "UST-3M-CMT", "UST-4M-CMT", "UST-6M-CMT", "UST-1Y-CMT",
        "UST-2Y-CMT", "UST-3Y-CMT", "UST-5Y-CMT", "UST-7Y-CMT", "UST-10Y-CMT", "UST-20Y-CMT", "UST-30Y-CMT",
    ]


def test_resolve_maps_every_source_key_and_lists_unknowns(seeded):
    ust = ["BC_1MONTH", "BC_1_5MONTH", "BC_2MONTH", "BC_3MONTH", "BC_4MONTH", "BC_6MONTH", "BC_1YEAR",
           "BC_2YEAR", "BC_3YEAR", "BC_5YEAR", "BC_7YEAR", "BC_10YEAR", "BC_20YEAR", "BC_30YEAR"]
    with db.session() as s:
        r = securities.resolve(s, "UST-PAR", [*ust, "BC_30YEARDISPLAY"], as_of=date(2026, 10, 1))
        h15 = securities.resolve(s, "H15-TCM", ["RIFLGFCM01_N.B", "RIFLGFCY30_N.B"])
    assert [m["value"] for m in r["matches"]] == ust and r["unknown"] == ["BC_30YEARDISPLAY"]
    assert len({m["sec_id"] for m in r["matches"]}) == 14
    assert [m["short_name"] for m in h15["matches"]] == ["UST-1M-CMT", "UST-30Y-CMT"]


def test_search(seeded):
    with db.session() as s:
        by_alias = securities.search(s, "6w")
        by_identifier = securities.search(s, "DGS10")
        by_text = securities.search(s, "20-year")
    assert [i["short_name"] for i in by_alias] == ["UST-1.5M-CMT"]
    assert [i["short_name"] for i in by_identifier] == ["UST-10Y-CMT"]
    assert [i["short_name"] for i in by_text] == ["UST-20Y-CMT"]


def test_search_pages_with_a_total(seeded):
    with db.session() as s:
        every = securities.search_page(s, "CMT", 100)
        first = securities.search_page(s, "CMT", 5)
        second = securities.search_page(s, "CMT", 5, offset=5)
    assert every["total"] == first["total"] == second["total"] == len(every["instruments"]) == 14
    names = [i["short_name"] for i in every["instruments"]]
    assert [i["short_name"] for i in first["instruments"] + second["instruments"]] == names[:10]


def test_tenor_days_orders_weeks_and_months():
    assert securities.tenor_days("P1M") < securities.tenor_days("P6W") < securities.tenor_days("P2M")


# --- Treasury securities for the screens (step 9) ---

def _loaded():
    from app import load
    from tests.test_load import NOW, TODAY, FakeMktData, _records

    mkt = FakeMktData()
    mkt.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    with db.session() as s:
        load.run(s, mkt, NOW, TODAY)
    return TODAY


def test_list_securities_by_maturity_with_on_the_run(migrated_db):
    today = _loaded()
    with db.session() as s:
        got = securities.list_securities(s, as_of=today)
        assert got["total"] == len(got["securities"]) >= 10
        mats = [x["maturity_date"] for x in got["securities"]]
        assert mats == sorted(mats) and all(x["status"] == "active" for x in got["securities"])
        note = next(x for x in got["securities"] if x["short_name"] == "UST-3.75-2033-02-28")
        assert note["security_type"] == "note" and note["coupon_rate"] == "0.0375" and note["cusip"] == "91282CQC8"
        assert any("OTR" in a for x in got["securities"] for a in x["on_the_run"])
        # Matured bills too, at most 3 listed: total counts them all.
        bills = securities.list_securities(s, security_type="bill", include_inactive=True, as_of=today, limit=3)
        assert {x["security_type"] for x in bills["securities"]} == {"bill"} and len(bills["securities"]) == 3
        assert bills["total"] > 3 and "matured" in {x["status"] for x in bills["securities"]}
        # A page at a time, in the same order.
        all_bills = securities.list_securities(s, security_type="bill", include_inactive=True, as_of=today)
        page2 = securities.list_securities(s, security_type="bill", include_inactive=True, as_of=today, limit=3, offset=3)
        assert page2["total"] == all_bills["total"]
        assert [x["cusip"] for x in page2["securities"]] == [x["cusip"] for x in all_bills["securities"][3:6]]
        later = securities.list_securities(s, maturing_from=date(2030, 1, 1), as_of=today)
        assert all(x["maturity_date"] >= "2030-01-01" for x in later["securities"])


def test_one_security_in_full(migrated_db):
    today = _loaded()
    with db.session() as s:
        got = securities.security(s, name="UST-3.75-2033-02-28", as_of=today)
        assert got["terms"]["cusip"] == "91282CQC8" and got["auctions"] and isinstance(got["on_the_run"], list)
        assert got["provenance"]["coupon_rate"].startswith("published")
        with pytest.raises(securities.UnknownInstrument):
            securities.security(s, name="NOPE")


def test_auction_results_overdue_in_the_metrics(migrated_db, monkeypatch):
    from app import metrics
    from app.models import Auction

    _loaded()  # the Feb 2026 TreasuryDirect capture: every auction there has results
    monkeypatch.setattr(securities, "today_ny", lambda: date(2026, 3, 2))
    with db.session() as s:
        assert "secmaster_svc_auction_results_overdue 0" in metrics.render(s)
        a = s.scalars(select(Auction).where(Auction.auction_date == date(2026, 2, 26))).first()
        a.total_accepted = None
        s.commit()
        text = metrics.render(s)
    assert "secmaster_svc_auction_results_overdue 1" in text
    assert f'cusip="{a.cusip}",auction_date="2026-02-26"' in text


def test_the_auction_calendar(migrated_db):
    _loaded()
    with db.session() as s:
        got = securities.list_auctions(s, date(2026, 2, 23), date(2026, 2, 27))
    rows = got["auctions"]
    assert rows and [r["auction_date"] for r in rows] == sorted(r["auction_date"] for r in rows)
    assert all("2026-02-23" <= r["auction_date"] <= "2026-02-27" for r in rows)
    note = next(r for r in rows if r["short_name"] == "UST-3.75-2033-02-28")
    assert note["security_type"] == "note" and note["reopening"] in ("true", "false") and note["total_accepted"]
