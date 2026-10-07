"""The load job (app/load.py): mkt-data's near-raw records into securities, against a fake mkt-data."""

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db, load, securities
from app import treasuries as tr
from app.models import Auction, Identifier, Instrument, InstrumentName, LoadRun, SecurityTerms, UntypedRecord
from app.upstream import Period, Rec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
TODAY = date(2026, 10, 7)


def _records(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


class FakeMktData:
    """mkt-data's Records service: periods of TreasuryDirect records, each with a capture id."""

    def __init__(self):
        self.periods: dict[str, tuple[int, list[dict]]] = {}
        self.reads: list[str] = []
        self._next = 1

    def put(self, period: str, rows: list[dict]) -> None:
        self._next += 1
        self.periods[period] = (self._next, rows)

    def list_periods(self, source):
        assert source == "TD-SECURITIES"
        return [Period(p, cap, len(rows)) for p, (cap, rows) in sorted(self.periods.items())]

    def get_period(self, source, period):
        self.reads.append(period)
        cap, rows = self.periods[period]
        return [Rec(i + 1, "auction", f"{r['cusip']}/{r['issueDate'][:10]}", r["auctionDate"][:10], r, cap)
                for i, r in enumerate(rows)]


@pytest.fixture
def mkt():
    m = FakeMktData()
    m.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    m.put("2026-10", _records("td_securities_2026_10_capture1261.json"))
    return m


def _run(m, now=NOW, today=TODAY):
    with db.session() as s:
        return load.run(s, m, now, today)


def test_first_load(migrated_db, mkt):
    out = _run(mkt)
    assert out["periods_read"] == 2 and out["added"] == 45 and out["created"] == 45 and out["untyped"] == 0
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SecurityTerms)) == 45
        assert s.scalar(select(func.count()).select_from(Identifier).where(Identifier.scheme == "ISIN")) == 45
        note = securities.get(s, name="UST-3.75-2033-02-28")
        assert note["type"] == "ust_note" and note["tenor"] == "P7Y" and note["status"] == "active"
        assert Decimal(note["terms"]["coupon_rate"]) == Decimal("0.0375")
        assert note["terms"]["penultimate_coupon_date"] == "2032-08-31"
        assert {i["scheme"] for i in note["identifiers"]} == {"CUSIP", "ISIN"}
        assert len(note["auctions"]) == 1 and Decimal(note["auctions"][0]["high_yield"]) == Decimal("0.0379")
        assert note["provenance"]["coupon_rate"].startswith("published: TD-SECURITIES 91282CQC8/")
        # Found by CUSIP too.
        assert securities.resolve(s, "CUSIP", ["91282CQC8"])["matches"][0]["short_name"] == "UST-3.75-2033-02-28"
        assert [i["short_name"] for i in securities.search(s, "91282CQC8")] == ["UST-3.75-2033-02-28"]
        types = dict(s.execute(select(Instrument.type, func.count()).group_by(Instrument.type)).all())
        rows = _records("td_securities_2026_02_capture1319.json") + _records("td_securities_2026_10_capture1261.json")
        want = Counter(tr.TYPES[t][0] for t in {r["cusip"]: r["type"] for r in rows}.values())
        assert types == want and types["ust_tips"] == 1 and types["ust_frn"] == 1
        assert s.scalars(select(LoadRun.outcome)).all() == ["ok"]


def test_nothing_new_reads_nothing(migrated_db, mkt):
    _run(mkt)
    mkt.reads.clear()
    out = _run(mkt)
    assert mkt.reads == [] and out["periods_skipped"] == 2 and out["terms_recorded"] == 0


def test_a_reopening_in_a_later_month_joins_its_security(migrated_db, mkt):
    """October reopens August's 30-year; when August arrives the original's terms take over, same sec_id."""
    _run(mkt)
    with db.session() as s:
        before = securities.get(s, name="UST-5.125-2056-08-15")
    assert before["terms"]["auction_date"] is None and len(before["auctions"]) == 1
    oct_bond = next(r for r in _records("td_securities_2026_10_capture1261.json") if r["cusip"] == "912810UW6")
    original = oct_bond | {"issueDate": "2026-08-17T00:00:00", "auctionDate": "2026-08-13T00:00:00",
                           "announcementDate": "2026-08-06T00:00:00", "reopening": "No",
                           "securityTerm": "30-Year", "highYield": "4.95"}
    mkt.put("2026-08", [original])
    out = _run(mkt, NOW + timedelta(hours=1))
    assert out["periods_read"] == 1 and out["created"] == 0 and out["terms_recorded"] == 1
    with db.session() as s:
        after = securities.get(s, name="UST-5.125-2056-08-15")
        history = s.scalars(select(SecurityTerms).where(SecurityTerms.sec_id == after["sec_id"])
                            .order_by(SecurityTerms.id)).all()
    assert after["sec_id"] == before["sec_id"] and len(after["auctions"]) == 2
    assert after["terms"]["auction_date"] == "2026-08-13" and after["checks"] == []
    assert [h.superseded_at is not None for h in history] == [True, False]


def test_results_update_an_announced_auction(migrated_db, mkt):
    rows = _records("td_securities_2026_10_capture1261.json")
    k = next(i for i, r in enumerate(rows) if r["cusip"] == "912810UW6")
    assert rows[k]["highYield"] == ""
    _run(mkt)
    rows[k] = rows[k] | {"highYield": "4.912", "bidToCoverRatio": "2.41"}
    mkt.put("2026-10", rows)
    out = _run(mkt)
    assert out["updated"] == 1 and out["terms_recorded"] == 0  # results don't change the terms
    with db.session() as s:
        a = s.scalar(select(Auction).where(Auction.source_key == "912810UW6/2026-10-15"))
    assert a.high_yield == Decimal("0.04912") and a.fields["bidToCoverRatio"] == "2.41"


def test_a_dropped_record_and_a_withdrawn_security(migrated_db, mkt):
    _run(mkt)
    rows = [r for r in _records("td_securities_2026_10_capture1261.json") if r["cusip"] != "912810UW6"]
    mkt.put("2026-10", rows)
    out = _run(mkt)
    assert out["removed"] == 1
    with db.session() as s:
        assert securities.get(s, name="UST-5.125-2056-08-15")["status"] == "withdrawn"


def test_untyped_records_are_reported_not_loaded(migrated_db, mkt):
    rows = _records("td_securities_2026_10_capture1261.json")
    rows[0] = rows[0] | {"type": "Swap"}
    mkt.put("2026-10", rows)
    out = _run(mkt)
    assert out["untyped"] == 1 and out["added"] == 44
    with db.session() as s:
        bad = s.scalars(select(UntypedRecord)).all()
        assert len(bad) == 1 and "Swap" in bad[0].problem
    rows[0] = rows[0] | {"type": "Bill"}
    mkt.put("2026-10", rows)
    _run(mkt)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(UntypedRecord)) == 0


def test_status_over_time_and_1980(migrated_db, mkt):
    mkt.put("1980-02", _records("td_securities_1980_02_capture1273.json"))
    _run(mkt)
    cusips = {r["cusip"] for r in _records("td_securities_1980_02_capture1273.json")}
    with db.session() as s:
        statuses = [securities.resolve(s, "CUSIP", [c])["matches"][0]["sec_id"] for c in cusips]
        got = sorted(s.get(Instrument, i).status for i in statuses)
        assert got == ["called"] + ["matured"] * 13
        assert securities.get(s, name="UST-11.75-2010-02-15")["status"] == "called"
        # February 2026's bills have matured by October; its notes haven't.
        assert securities.get(s, name="UST-3.75-2033-02-28")["status"] == "active"


def test_short_name_collision_gets_the_cusip(migrated_db, mkt):
    rows = _records("td_securities_2026_10_capture1261.json")
    bill = next(r for r in rows if r["type"] == "Bill")
    twin = bill | {"cusip": "912797ZZ1", "issueDate": bill["issueDate"], "cashManagementBillCMB": "Yes"}
    mkt.put("2026-10", [*rows, twin])
    _run(mkt)
    with db.session() as s:
        names = set(s.scalars(select(InstrumentName.name).where(InstrumentName.kind == "short")))
    mat = bill["maturityDate"][:10]
    assert f"UST-B-{mat}" in names and f"UST-B-{mat}-912797ZZ1" in names


def test_rebuild_keeps_sec_ids(migrated_db, mkt):
    _run(mkt)
    with db.session() as s:
        before = dict(s.execute(select(Identifier.value, Identifier.sec_id).where(Identifier.scheme == "CUSIP")).all())
        out = load.rebuild(s, mkt, NOW + timedelta(days=1), TODAY)
        after = dict(s.execute(select(Identifier.value, Identifier.sec_id).where(Identifier.scheme == "CUSIP")).all())
    assert out["periods_read"] == 2 and out["created"] == 0 and before == after


def test_a_failed_load_is_recorded(migrated_db, mkt):
    def broken(source):
        raise RuntimeError("mkt-data unreachable")

    mkt.list_periods = broken
    with pytest.raises(load.LoadError, match="unreachable"):
        _run(mkt)
    with db.session() as s:
        assert s.scalars(select(LoadRun.outcome)).all() == ["error"]
