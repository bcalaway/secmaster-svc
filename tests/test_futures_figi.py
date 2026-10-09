"""FIGIs, tickers and root checks for listed futures (app/futures_figi.py), against a fake OpenFIGI."""

from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app import db, futures_figi, securities
from app.models import FuturesFigiLookup, Identifier
from tests.test_futures_load import NOW, TODAY
from tests.test_futures_load import run as generate

MISSING = {"warning": "No identifier found."}
DEC26 = date(2026, 12, 1)


def row(ticker, sector="Comdty", figi="BBG000000001", name="US 10YR NOTE (CBT)Dec26"):
    return {"figi": figi, "compositeFIGI": None, "ticker": ticker, "marketSector": sector, "name": name,
            "securityType": "Financial commodity future.", "securityType2": "Future", "exchCode": "CBT"}


def test_tickers():
    assert futures_figi.live_ticker("TY", DEC26) == "TYZ6"
    assert futures_figi.parse_ticker("SFRH7 Comdty") == ("SFR", "H", "7")
    assert futures_figi.parse_ticker("TYZ26") == ("TY", "Z", "26")
    assert futures_figi.parse_ticker("T 4 1/4 08/15/35") is None
    assert futures_figi.jobs("TY", "ZNZ6", DEC26, "treasury") == [
        {"idType": "TICKER", "idValue": "TYZ6", "marketSecDes": "Comdty", "securityType2": "Future"},
        {"idType": "TICKER", "idValue": "TYZ26", "marketSecDes": "Comdty", "securityType2": "Future"},
        {"idType": "ID_EXCH_SYMBOL", "idValue": "ZNZ6", "securityType2": "Future"}]
    assert futures_figi.jobs("BP", "6BV6", date(2026, 10, 1), "fx")[0]["marketSecDes"] == "Curncy"


def test_product_name():
    assert futures_figi.product_name("US 10YR NOTE (CBT)Dec26") == "US 10YR NOTE (CBT)"
    assert futures_figi.product_name("BP CURRENCY FUT   Oct26") == "BP CURRENCY FUT"
    assert futures_figi.product_name(None) == "?"


def test_judge():
    j = futures_figi.judge
    both = j("TY", DEC26, {"data": [row("TYZ6")]}, {"data": [row("TYZ6")]})
    assert (both.outcome, both.via, both.ticker, both.figi) == ("confirmed", "both", "TYZ6 Comdty", "BBG000000001")
    assert j("TY", DEC26, MISSING, {"data": [row("TYZ6")]}).via == "exchange"
    assert j("EC", DEC26, {"data": [row("ECZ6", "Curncy")]}, MISSING).ticker == "ECZ6 Curncy"
    # CME's code maps to another root: Bloomberg's, which the seed should use.
    got = j("SER", DEC26, MISSING, {"data": [row("SOZ6")]})
    assert (got.outcome, got.bloomberg_root) == ("mismatch", "SO")
    # A ticker that's another month or year doesn't count.
    assert j("TY", DEC26, {"data": [row("TYH7")]}, MISSING).outcome == "not_found"
    assert j("TY", DEC26, {"error": "Invalid idValue format"}, MISSING).outcome == "error"
    assert j("TY", DEC26, MISSING, MISSING).outcome == "not_found"
    # Outside the product's sector a ticker is another product: BPV6 Comdty is No. 2 soybeans.
    soy = {"data": [row("BPV6", name="NO.2 SOYBEAN      Oct26")]}
    assert j("BP", date(2026, 10, 1), soy, MISSING, "Curncy").outcome == "not_found"
    assert j("BP", date(2026, 10, 1), [MISSING, {"data": [row("BPV6", "Curncy")]}], MISSING, "Curncy").via == "ticker"
    # A two-digit year counts as the contract's.
    assert j("SFR", date(2031, 12, 1), [MISSING, {"data": [row("SFRZ31")]}], MISSING, "Comdty").ticker == "SFRZ31 Comdty"


class FakeFigi:
    """Knows every contract by ticker (in the sector asked) and by CME code, except as told."""

    def __init__(self, roots=None, unknown=(), figi_prefix="BBG"):
        self.roots = roots or {}  # our root -> Bloomberg's, for CME-code answers
        self.unknown = set(unknown)  # our roots OpenFIGI doesn't know at all
        self.prefix = figi_prefix
        self.calls: list[list[dict]] = []

    def __call__(self, jobs, api_key):
        self.calls.append(jobs)
        out = []
        for i in range(0, len(jobs), 3):
            one = jobs[i]
            ours, sector = one["idValue"], one["marketSecDes"]
            root, rest = ours[:-2], ours[-2:]
            figi = f"{self.prefix}{abs(hash(ours)) % 10**9:09d}"
            if root in self.unknown:
                out += [MISSING, MISSING, MISSING]
            elif root in self.roots:
                out += [MISSING, MISSING, {"data": [row(self.roots[root] + rest, figi=figi)]}]
            else:
                out += [{"data": [row(ours, sector, figi)]}, MISSING, {"data": [row(ours, sector, figi)]}]
        return out


def ask(mapper, now=NOW, key="k"):
    with db.session() as s:
        return futures_figi.run(s, key, now=now, mapper=mapper)


def test_run_stores_figis_and_tickers(migrated_db):
    generate()
    fake = FakeFigi(roots={"SER": "SER1"}, unknown={"NV"})
    out = ask(fake)
    assert out["asked"] == 500 and len(fake.calls[0]) == 3 * 500
    assert out["products"]["TY"]["confirmed"] == 3 and out["products"]["TY"]["via"] == {"both": 3}
    assert out["products"]["SER"]["mismatch"] == 25 and out["products"]["SER"]["bloomberg_roots"] == ["SER1"]
    assert out["products"]["NV"]["not_found"] == 6
    assert "TY" in out["roots_found"] and "SER" not in out["roots_found"]
    assert out["products"]["TY"]["example"]["exch_code"] == "CBT"
    assert out["products"]["TY"]["names"] == {"US 10YR NOTE (CBT) | Comdty | CBT": 3}
    assert out["products"]["NV"]["first_not_found"]["said"][0] == "NVZ6: No identifier found."
    with db.session() as s:
        got = securities.get(s, name="TYZ26")
        ids = {i["scheme"]: i for i in got["identifiers"]}
        assert ids["TICKER"]["value"] == "TYZ6 Comdty" and ids["FIGI"]["value"].startswith("BBG")
        assert ids["TICKER"]["valid_from"] == ids["CME"]["valid_from"]
        assert ids["TICKER"]["valid_to"] == "2026-12-21"
        assert securities.resolve(s, "TICKER", ["ECZ6 Curncy"], as_of=TODAY)["matches"][0]["short_name"] == "ECZ26"
        assert not any(i["scheme"] == "TICKER" for i in securities.get(s, name="SERZ26")["identifiers"])


def test_confirmed_contracts_arent_asked_again(migrated_db):
    generate()
    ask(FakeFigi(unknown={"NV"}))
    fake = FakeFigi()
    out = ask(fake)
    assert out["asked"] == 6 and out["tickers_added"] == 6  # only NV's, now found
    out = ask(fake)
    assert out["asked"] == 0 and out["tickers_added"] == out["tickers_changed"] == 0
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(FuturesFigiLookup)) == 500


def test_new_listing_and_expiry(migrated_db):
    generate()
    ask(FakeFigi())
    later = datetime(2026, 12, 22, 12, tzinfo=UTC)
    generate(today=date(2026, 12, 22), now=later)
    out = ask(FakeFigi(), now=later)
    assert out["asked"] > 0  # the months listed since
    with db.session() as s:
        # TYZ6 stopped trading on Dec 21: its ticker still holds to then, and resolves as of a day it traded.
        got = securities.resolve(s, "TICKER", ["TYZ6 Comdty"], as_of=date(2026, 12, 21))
        assert [m["short_name"] for m in got["matches"]] == ["TYZ26"]
        assert not securities.resolve(s, "TICKER", ["TYZ6 Comdty"], as_of=date(2026, 12, 22))["matches"]


def test_without_a_key_asks_a_hundred(migrated_db):
    generate()
    out = ask(FakeFigi(), key=None)
    assert out["asked"] == 100 and out["left"] == 400 and not out["with_key"]


def test_job_dag_report_and_metrics(migrated_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import figi, jobs
    from app.config import Settings
    from app.main import app

    generate()
    client = TestClient(app)
    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t", openfigi_api_key="k"))
    monkeypatch.setattr(figi, "map_jobs", FakeFigi(unknown={"NV"}))
    auth = {"Authorization": "Bearer t"}
    assert client.post("/jobs/futures-figi").status_code == 401
    out = client.post("/jobs/futures-figi", headers=auth).json()
    assert out["confirmed"] == 494
    body = client.get("/metrics").text
    assert 'secmaster_svc_futures_figi_lookups{product="TY",outcome="confirmed"} 3' in body
    assert 'secmaster_svc_futures_figi_lookups{product="NV",outcome="not_found"} 6' in body

    def down(jobs, api_key):
        raise figi.FigiError("OpenFIGI unreachable: no route")

    monkeypatch.setattr(figi, "map_jobs", down)
    assert client.post("/jobs/futures-figi", headers=auth).status_code == 502  # NV's 6 are still to ask


def test_dag_report(capsys):
    import importlib.util
    import sys
    import types
    from pathlib import Path

    class Task:  # stands in for a task's output: the DAG only chains them
        def __rshift__(self, other):
            return other

    def task(*a, **k):
        def wrap(f):
            return lambda *x, **y: Task()
        return wrap(a[0]) if a and callable(a[0]) else wrap

    sdk = types.ModuleType("airflow.sdk")
    sdk.CronTriggerTimetable = lambda *a, **k: None
    sdk.dag = lambda **k: (lambda f: f)
    sdk.task = task
    jobs_mod = types.ModuleType("home_platform_jobs")
    jobs_mod.call_app_job = lambda *a, **k: {}
    saved = {k: sys.modules.get(k) for k in ("airflow", "airflow.sdk", "home_platform_jobs")}
    sys.modules.update({"airflow": types.ModuleType("airflow"), "airflow.sdk": sdk, "home_platform_jobs": jobs_mod})
    try:
        path = Path(__file__).resolve().parent.parent / "dags" / "futures.py"
        spec = importlib.util.spec_from_file_location("futures_dag", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    left = mod.report_figi({"asked": 2, "products": {
        "SER": {"listed": 25, "confirmed": 0, "via": {}, "mismatch": 25, "bloomberg_roots": ["SO"], "not_found": 0,
                "error": 0}}})
    assert "products" not in left
    assert "SER: 0 of 25 confirmed {}, 25 mismatch, 0 not found, 0 error; Bloomberg's root: SO" in capsys.readouterr().out


def test_a_newer_check_asks_again_and_retires_what_it_got_wrong(migrated_db):
    """The first run (CHECK 1) took commodity futures for three FX products; the next retires them."""
    generate()
    ask(FakeFigi(figi_prefix="BAD"))
    with db.session() as s:
        for r in s.scalars(select(FuturesFigiLookup)):
            r.detail = {**(r.detail or {}), "check": 1}
        s.commit()
    out = ask(FakeFigi(unknown={"NV"}))
    assert out["asked"] == 500 and out["retired_count"] == 500 and len(out["retired"]) == 20  # listed to 20
    with db.session() as s:
        got = {i["scheme"]: i["value"] for i in securities.get(s, name="TYZ26")["identifiers"]}
        assert got["FIGI"].startswith("BBG") and got["TICKER"] == "TYZ6 Comdty"
        nv = {i["scheme"] for i in securities.get(s, name="NVZ26")["identifiers"]}
        assert "FIGI" not in nv and "TICKER" not in nv
        bad = s.scalar(select(func.count()).select_from(Identifier).where(
            Identifier.value.like("BAD%"), Identifier.removed_at.is_(None)))
        assert bad == 0
