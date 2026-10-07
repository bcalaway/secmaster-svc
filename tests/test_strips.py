"""Treasury STRIPS (app/strips.py and the load's sync), on real records."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db, load, securities, strips
from app.models import Instrument, Strip, StrippedAmount
from tests.test_load import NOW, TODAY, FakeMktData, _records

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MSPD = json.loads((FIXTURES / "fd_mspd_strips_2026_09_capture1264.json").read_text())["data"]


def test_interest_strips_from_a_record():
    tips = next(r for r in _records("td_securities_2026_02_capture1319.json") if r["cusip"] == "912810US5")
    got = strips.from_auction("912810US5", "tips", date(2056, 2, 15), tips, "912810US5/2026-02-27")
    assert [(x.cusip, x.payment_date, x.tips) for x in got] == [
        ("912834ZQ4", date(2055, 8, 15), True), ("912834ZR2", date(2056, 2, 15), True)]
    assert [strips.short_name(x) for x in got] == ["UST-SI-TII-2055-08-15", "UST-SI-TII-2056-02-15"]


def test_an_interest_strip_without_a_due_date_takes_the_maturity():
    note = next(r for r in _records("td_securities_2026_10_capture1261.json") if r["cusip"] == "91282CRQ6")
    assert note["tintCusip1"] == "912834L89" and note["tintCusip1DueDate"] == ""
    (got,) = strips.from_auction("91282CRQ6", "note", date(2029, 10, 15), note, "91282CRQ6/2026-10-15")
    assert got.payment_date == date(2029, 10, 15) and got.provenance["payment_date"].startswith("derived:")


def test_mspd_line():
    line = next(r for r in MSPD if r["cusip"] == "912803BJ1")
    spec = strips.from_mspd(line, "912803BJ1/2026-09-30")
    assert (spec.kind, spec.underlying_cusip, spec.payment_date, spec.tips) == (
        "principal", "912810EY0", date(2026, 11, 15), False)
    assert strips.short_name(spec) == "UST-SP-2026-11-15"
    tips = next(r for r in MSPD if r["security_class1_desc"].startswith("Treasury Inflation"))
    assert strips.short_name(strips.from_mspd(tips, "k")).startswith("UST-SP-TII-")
    assert strips.from_mspd(next(r for r in MSPD if r["cusip"] == "null"), "k") is None


def test_sources_merge_and_disagree():
    a = strips.StripSpec("912821TY1", "principal", False, date(2033, 2, 28), "91282CQC8",
                         {"cusip": "published: TD-SECURITIES k corpusCusip"})
    b = strips.StripSpec("912821TY1", "principal", False, date(2033, 2, 27), "91282CQC8",
                         {"cusip": "published: FD-MSPD-STRIPS k cusip"})
    merged = strips.merge([b, a])["912821TY1"]
    assert merged.payment_date == date(2033, 2, 28)  # TreasuryDirect's wins
    assert merged.checks == ["sources-disagree: on payment_date: 2033-02-28 and 2033-02-27"]


@pytest.fixture
def mkt():
    m = FakeMktData()
    m.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    m.put("2026-10", _records("td_securities_2026_10_capture1261.json"))
    m.put("2026-09", MSPD, source="FD-MSPD-STRIPS")
    return m


def test_load_builds_strips(migrated_db, mkt):
    with db.session() as s:
        out = load.run(s, mkt, NOW, TODAY)
    principal_in_mspd = {r["cusip"] for r in MSPD if r["cusip"] != "null"}
    assert out["untyped"] == 0 and out["strips_created"] == out["strips"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(StrippedAmount)) == 406  # the 4 totals aren't kept
        kinds = dict(s.execute(select(Strip.kind, func.count()).group_by(Strip.kind)).all())
        # Every MSPD line's principal STRIPS, plus February's and October's new ones from corpusCusip.
        assert kinds["principal"] >= len(principal_in_mspd) and kinds["interest"] == 5
        # The new 7-year's principal STRIPS, from TreasuryDirect's corpusCusip, linked to its note.
        p = securities.get(s, name="UST-SP-2033-02-28")
        assert p["type"] == "ust_strip_principal" and p["strip"]["underlying_cusip"] == "91282CQC8"
        assert "UST-3.75-2033-02-28" in p["description"] and {i["scheme"] for i in p["identifiers"]} == {"CUSIP", "ISIN"}
        i = securities.get(s, name="UST-SI-TII-2056-02-15")
        assert i["type"] == "ust_strip_interest" and i["strip"]["tips"] is True
        # An MSPD line whose security predates what's loaded: kept, linked by CUSIP only, flagged.
        old = s.scalar(select(Strip).where(Strip.cusip == "912803BJ1"))
        assert old.underlying_sec_id is None and old.checks == ["underlying-not-loaded: its security isn't in the security master"]
        # Amounts are dollars: the MSPD prints thousands.
        amt = s.scalar(select(StrippedAmount).where(StrippedAmount.strip_cusip == "912803BJ1"))
        assert amt.stripped == Decimal(1143486600) and amt.record_date == date(2026, 9, 30)
        assert s.get(Instrument, old.sec_id).status == "active"


def test_strips_are_stable_across_loads(migrated_db, mkt):
    with db.session() as s:
        load.run(s, mkt, NOW, TODAY)
        out = load.run(s, mkt, NOW, TODAY)
    assert out["strips_created"] == 0 and out["strips_updated"] == 0
