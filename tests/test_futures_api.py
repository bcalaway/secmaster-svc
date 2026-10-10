"""Futures for the screens and voice (app/futures_api.py; mkt-data's docs/phase-4.md, step 7)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app import db, futures_api
from app.models import StrippedAmount
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


def _mspd(s, cusip: str, record_date: date, outstanding: str, unstripped: str, key: str):
    s.add(StrippedAmount(period=record_date.isoformat()[:7], source_key=key, strip_cusip="912803ZZ9",
                         underlying_cusip=cusip, record_date=record_date, outstanding=Decimal(outstanding),
                         unstripped=Decimal(unstripped), stripped=Decimal(outstanding) - Decimal(unstripped),
                         reconstituted=Decimal(0), fields={}, record_id=1, capture_id=1,
                         loaded_at=datetime(2026, 10, 10, tzinfo=UTC)))


def test_a_basket_shows_amounts_outstanding_from_mspds_latest_month(migrated_db):
    with db.session() as s:
        _add_note(s, 900002, "91282CLB2", "0.0425", date(2024, 11, 15), date(2034, 11, 15))  # in TY's basket
        _add_note(s, 900003, "91282CHT1", "0.0375", date(2023, 8, 15), date(2033, 8, 15))  # in it, not in MSPD
        _mspd(s, "91282CLB2", date(2026, 8, 31), "125000000000", "120000000000", "a")
        _mspd(s, "91282CLB2", date(2026, 9, 30), "126500000000", "121000000000", "b")  # the latest month wins
        s.commit()
    run()
    with db.session() as s:
        b = futures_api.basket(s, "TYZ26")
    by = {d["cusip"]: d for d in b["deliverables"]}
    assert (by["91282CLB2"]["outstanding"], by["91282CLB2"]["unstripped"]) == ("126500000000", "121000000000")
    assert by["91282CLB2"]["outstanding_as_of"] == "2026-09-30"
    assert by["91282CHT1"]["outstanding"] == by["91282CHT1"]["outstanding_as_of"] == ""
    assert by["91282CHT1"]["valid_from"] == "2023-08-15"  # joined on its issue date
    assert (b["outstanding_total"], b["unstripped_total"]) == ("126500000000", "121000000000")


def test_the_contracts_a_security_is_deliverable_into(migrated_db):
    with db.session() as s:
        _add_note(s, 900002, "91282CLB2", "0.0425", date(2024, 11, 15), date(2034, 11, 15))
        s.commit()
    run()
    with db.session() as s:
        got = futures_api.deliverable_into(s, 900002)
        none = futures_api.deliverable_into(s, 1)
    assert next(g["contract"] for g in got if g["product"] == "TY") == "TYZ26"
    z6 = next(g for g in got if g["contract"] == "TYZ26")
    assert (z6["month"], z6["status"], z6["remaining_months"]) == ("2026-12", "listed", 93)
    assert z6["conversion_factor"] and z6["valid_from"] == "2024-11-15" and z6["last_delivery_date"]
    assert got == sorted(got, key=lambda g: (g["product"], g["month"]))
    assert none == []
