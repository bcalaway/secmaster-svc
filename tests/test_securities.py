"""Lookups (app/securities.py): what quote-svc and mkt-api ask."""

from datetime import date
from pathlib import Path

import pytest

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


def test_tenor_days_orders_weeks_and_months():
    assert securities.tenor_days("P1M") < securities.tenor_days("P6W") < securities.tenor_days("P2M")
