"""Futures for the screens and voice (app/futures_api.py; mkt-data's docs/phase-4.md, step 7)."""

from datetime import date

import pytest

from app import db, futures_api
from app.securities import UnknownInstrument
from tests.test_baskets import _add_note
from tests.test_futures_load import TODAY, run


def test_products_list_every_product_with_its_front(migrated_db):
    run()
    with db.session() as s:
        got = {p["root"]: p for p in futures_api.products(s, TODAY)}
    assert len(got) == 75
    assert got["TY"]["front"] == "TYZ26" and got["TY"]["cme_code"] == "ZN"
    assert got["TY"]["kind"] == "treasury" and got["TY"]["cftc_code"] == "043602"
    assert got["3Y"]["cftc_code"] == ""  # the CFTC doesn't report the 3-year
    assert got["EC"]["front"] == "ECZ26" and got["EC"]["status"] == "listed"


def test_a_product_with_its_contracts_and_generics(migrated_db):
    run()
    with db.session() as s:
        ty = futures_api.product(s, "ty", on=TODAY)
        every = futures_api.product(s, "TY", include_expired=True, on=TODAY)
    assert [c["name"] for c in ty["contracts"]] == ["TYZ26", "TYH27", "TYM27"]
    z6 = ty["contracts"][0]
    assert (z6["cme_code"], z6["month"], z6["status"]) == ("ZNZ6", "2026-12", "listed")
    assert (z6["last_trade_date"], z6["first_notice_date"]) == ("2026-12-21", "2026-11-30")
    assert z6["basket_size"] == 0  # no securities loaded in this test
    assert ty["generics"][0] == {"generic": "TY1", "contract": "TYZ26"}
    assert "last_bd_minus:7" in ty["rules"]["last_trade_date"] and ty["rule_sources"]["last_trade_date"]
    assert ty["basket_rule"].startswith("remaining >= 6y6m")
    assert len(every["contracts"]) > 100 and every["contracts"][0]["status"] == "expired"
    with pytest.raises(UnknownInstrument):
        futures_api.product(s, "NOPE")


def test_a_basket_with_conversion_factors(migrated_db):
    with db.session() as s:
        _add_note(s, 900002, "91282CLB2", "0.0425", date(2024, 11, 15), date(2034, 11, 15))  # 10-year: TY
        s.commit()
    run()
    with db.session() as s:
        b = futures_api.basket(s, "TYZ26")
        ty = futures_api.product(s, "TY", on=TODAY)
    assert (b["contract"], b["product"], b["month"]) == ("TYZ26", "TY", "2026-12")
    [d] = b["deliverables"]
    assert d["cusip"] == "91282CLB2" and d["coupon_rate"] == "0.0425" and d["maturity_date"] == "2034-11-15"
    assert d["remaining_months"] == 93 and d["conversion_factor"]
    assert ty["contracts"][0]["basket_size"] == 1
    with pytest.raises(UnknownInstrument):
        futures_api.basket(s, "NOPE")
