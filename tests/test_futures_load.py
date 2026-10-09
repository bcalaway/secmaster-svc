"""Storing futures (app/futures_load.py): products, contracts, names, CME codes and generics."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app import calendars, db, futures_load, futures_seed, securities
from app.models import FuturesContract, FuturesProduct, FuturesRun, FuturesSpec, Identifier, Instrument
from tests.test_futures import TARGET_2026, US_2026

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
TODAY = date(2026, 10, 8)
NAMES = ("CME-IR", "CME-FX", "FED", "TARGET", "JP", "GB", "AU", "CA", "CH", "MX", "NZ", "NO", "SE",
         "BR", "CL", "CN", "CZ", "HK", "HU", "ID", "IL", "IN", "KR", "PL", "SG", "TH", "TR", "ZA")


class FakeCalendars:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def years(self):
        return {n: (1999 if n == "TARGET" else 1990, 2100) for n in NAMES if n not in self.missing}

    def closed(self, calendar, start, end):
        self.calls.append(calendar)
        days = TARGET_2026 if calendar == "TARGET" else US_2026
        return {d for d in days if start <= d <= end}


def run(today=TODAY, now=NOW, source=None, seed=None):
    with db.session() as s:
        return futures_load.run(s, source or FakeCalendars(), now=now, today=today, seed=seed)


def test_first_run_builds_everything(migrated_db):
    src = FakeCalendars()
    out = run(source=src)
    assert out["products_created"] == 75 and out["contracts_created"] > 2000
    assert sorted(src.calls) == sorted(NAMES)  # each calendar once
    assert out["products"]["TY"] == {"contracts": 150, "listed": 3, "front": "TYZ26"}
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(FuturesProduct)) == 75
        assert s.scalar(select(func.count()).select_from(FuturesSpec)) == 75
        runs = list(s.scalars(select(FuturesRun)))
        assert [r.outcome for r in runs] == ["ok"]


def test_second_run_changes_nothing(migrated_db):
    run()
    out = run()
    assert not out["changed"], out


def test_contract_lookup(migrated_db):
    run()
    with db.session() as s:
        got = securities.get(s, name="TYZ26")
        assert got["type"] == "fut_treasury" and got["status"] == "active"
        assert got["description"] == "10-Year T-Note Futures, December 2026"
        c = got["contract"]
        assert c["product"] == "TY" and c["contract_month"] == "2026-12-01"
        assert c["last_trade_date"] == "2026-12-21" and c["first_notice_date"] == "2026-11-30"
        assert c["status"] == "listed"
        assert "last_bd_minus:7" in got["provenance"]["last_trade_date"]
        cme = [i for i in got["identifiers"] if i["scheme"] == "CME"]
        assert cme == [{"scheme": "CME", "value": "ZNZ6", "valid_from": c["first_trade_date"],
                        "valid_to": "2026-12-21"}]
        product = securities.get(s, name="TY")
        assert product["type"] == "fut_product" and product["futures_product"]["cme_code"] == "ZN"
        assert product["specs"][0]["source"].startswith("https://www.cmegroup.com/")


def test_generics_resolve_by_date(migrated_db):
    run()
    with db.session() as s:
        assert securities.get(s, name="TY1", as_of=TODAY)["short_name"] == "TYZ26"
        # The September contract was the front through the day before its first intention day.
        assert securities.get(s, name="TY1", as_of=date(2026, 8, 27))["short_name"] == "TYU26"
        assert securities.get(s, name="TY1", as_of=date(2026, 8, 28))["short_name"] == "TYZ26"
        # Generics are recorded to today's: the next roll isn't there until it happens.
        with pytest.raises(securities.UnknownInstrument):
            securities.get(s, name="TY1", as_of=date(2026, 11, 27))
        assert securities.get(s, name="EC1", as_of=TODAY)["short_name"] == "ECZ26"
        assert securities.get(s, name="TY1", as_of=date(1995, 2, 1))["short_name"] == "TYH95"


def test_cme_codes_dont_overlap_when_they_come_round(migrated_db):
    run()
    with db.session() as s:
        got = securities.resolve(s, "CME", ["ZNZ6"], as_of=date(2016, 12, 1))
        assert [m["short_name"] for m in got["matches"]] == ["TYZ16"]
        got = securities.resolve(s, "CME", ["ZNZ6"], as_of=TODAY)
        assert [m["short_name"] for m in got["matches"]] == ["TYZ26"]


def test_next_day_rolls_status_and_keeps_first_trade(migrated_db):
    run()
    with db.session() as s:
        before = s.scalar(select(FuturesContract).join(Instrument, Instrument.sec_id == FuturesContract.sec_id)
                          .where(FuturesContract.contract_month == date(2026, 12, 1),
                                 FuturesContract.superseded_at.is_(None), Instrument.type == "fut_treasury",
                                 Instrument.description.like("10-Year%")))
        first_trade = before.first_trade_date
        sec_id = before.sec_id
    out = run(today=date(2027, 1, 5), now=datetime(2027, 1, 5, 12, tzinfo=UTC))
    assert out["contracts_changed"] > 0
    with db.session() as s:
        row = s.scalar(select(FuturesContract).where(FuturesContract.sec_id == sec_id,
                                                     FuturesContract.superseded_at.is_(None)))
        assert row.status == "expired" and row.first_trade_date == first_trade
        assert s.get(Instrument, sec_id).status == "expired"
        history = s.scalar(select(func.count()).select_from(FuturesContract).where(FuturesContract.sec_id == sec_id))
        assert history == 2  # the first row is kept, superseded


def test_root_change_renames_and_keeps_sec_ids(migrated_db):
    run()
    with db.session() as s:
        sec_id = securities.get(s, name="SERZ26")["sec_id"]
    body = futures_seed.PATH.read_text().replace('root = "SER"', 'root = "SOX"', 1)
    run(seed=futures_seed.parse(body.encode()))
    with db.session() as s:
        got = securities.get(s, name="SOXZ26")
        assert got["sec_id"] == sec_id and "SERZ26" in got["aliases"]
        assert securities.get(s, name="SOX1", as_of=TODAY)["sec_id"]
        assert not securities.resolve(s, "GENERIC", ["SER1"])["matches"]


def test_missing_calendar_fails_and_changes_nothing(migrated_db):
    with pytest.raises(futures_load.FuturesError, match="no calendar NZ"):
        run(source=FakeCalendars(missing={"NZ"}))
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(FuturesProduct)) == 0
        assert [r.outcome for r in s.scalars(select(FuturesRun))] == ["error"]


def test_fetch_clamps_to_coverage():
    got = calendars.fetch(FakeCalendars(), ["TARGET"], date(1990, 1, 1), date(2040, 12, 31))
    assert got.years["TARGET"] == (1999, 2100) and date(2026, 12, 25) in got.closed["TARGET"]


def test_instrument_seed_skips_the_futures_seed(migrated_db):
    from app import seed

    with db.session() as s:
        names = [r["seed"] for r in seed.apply_all(s)]
    assert names == ["cmt"]


class _FakeGrpc(FakeCalendars):
    def __init__(self, target, missing=()):
        super().__init__(missing)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_job_and_metrics(migrated_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import calendars as cal_mod
    from app import jobs
    from app.config import Settings
    from app.main import app

    client = TestClient(app)
    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t"))
    monkeypatch.setattr(cal_mod, "GrpcCalendars", _FakeGrpc)
    auth = {"Authorization": "Bearer t"}
    assert client.post("/jobs/futures").status_code == 401
    out = client.post("/jobs/futures", headers=auth).json()
    assert out["products"]["TY"]["listed"] == 3
    body = client.get("/metrics").text
    assert 'secmaster_svc_futures_contracts{product="TY",status="listed"} 3' in body
    assert "secmaster_svc_futures_ok 1" in body
    monkeypatch.setattr(cal_mod, "GrpcCalendars", lambda target: _FakeGrpc(target, missing={"NZ"}))
    assert client.post("/jobs/futures", headers=auth).status_code == 502
    assert "secmaster_svc_futures_ok 0" in client.get("/metrics").text


def test_futures_identifiers_are_current_only(migrated_db):
    run()
    with db.session() as s:
        n = s.scalar(select(func.count()).select_from(Identifier).where(
            Identifier.scheme == "GENERIC", Identifier.removed_at.is_(None)))
        assert n > 10000



def test_a_corrected_rule_redates_and_withdraws(migrated_db, monkeypatch):
    """Step 2a dated SR3 by the month its reference quarter ends in; CME names it by the month it starts."""
    from app import futures

    # The old convention, as first deployed: reference quarter ending in the contract month, 6 nearest serials.
    def old(c, cals_):
        d = real(c, cals_)
        if c.product.root == "SFR":
            d.dates["reference_start"] = futures.third_wednesday(futures._add_months(c.month, -3))
            d.dates["reference_end"] = futures.third_wednesday(c.month)
            d.dates["last_trade_date"] = futures._step(cals_, c.product.trade_calendars, d.dates["reference_end"], -1)
        return d

    real = futures.dates_for
    body = futures_seed.PATH.read_text().replace("listing = { quarterly = 39, serial_months = 7 }",
                                                 "listing = { quarterly = 39, serial_nearest = 6 }", 1)
    monkeypatch.setattr(futures, "dates_for", old)
    run(seed=futures_seed.parse(body.encode()))
    with db.session() as s:
        k7 = securities.get(s, name="SFRK27")["sec_id"]
        z6 = securities.get(s, name="SFRZ26")
        assert z6["contract"]["last_trade_date"] == "2026-12-15"
    monkeypatch.setattr(futures, "dates_for", real)
    out = run()
    # Withdrawn: SFRK27 (not a listed serial) and SFRM36 (the 40th quarter); new: SFRN26, SFRQ26, SFRU26.
    assert out["contracts_withdrawn"] == 2 and out["contracts_created"] == 3
    with db.session() as s:
        z6 = securities.get(s, name="SFRZ26")
        assert z6["contract"]["last_trade_date"] == "2027-03-16" and z6["sec_id"]
        assert z6["contract"]["reference_start"] == "2026-12-16"
        k = securities.get(s, sec_id=k7)
        assert k["status"] == "withdrawn" and k["contract"]["status"] == "withdrawn"
        assert not [i for i in k["identifiers"] if i["scheme"] in ("CME", "GENERIC")]
        assert securities.get(s, name="SFR1", as_of=TODAY)["short_name"] == "SFRU26"
        assert securities.resolve(s, "CME", ["SR3U6"], as_of=TODAY)["matches"][0]["short_name"] == "SFRU26"
    assert not run()["changed"]


def test_a_serial_month_expires_when_it_stops_trading(migrated_db):
    run()
    later = date(2026, 10, 21)  # SR3N6 and TBF3V6 stopped trading on the 20th and 19th
    out = run(today=later, now=datetime(2026, 10, 21, 12, tzinfo=UTC))
    assert out["contracts_expired"] >= 2 and out["contracts_withdrawn"] == 0
    with db.session() as s:
        for name in ("SFRN26", "TZRV26"):
            got = securities.get(s, name=name)
            assert got["status"] == "expired" and got["contract"]["status"] == "expired"
