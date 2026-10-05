"""The job API (app/jobs.py) and metrics (app/metrics.py)."""

from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer t"}


def _tokens(monkeypatch, airflow="t", read=None):
    monkeypatch.setattr(jobs, "settings", Settings(airflow_token=airflow, read_token=read))


def test_seed_needs_the_token(migrated_db, monkeypatch):
    _tokens(monkeypatch)
    assert client.post("/jobs/seed").status_code == 401
    _tokens(monkeypatch, airflow=None)
    assert client.post("/jobs/seed", headers=AUTH).status_code == 503


def test_seed_then_lookups(migrated_db, monkeypatch):
    _tokens(monkeypatch, read="r")
    out = client.post("/jobs/seed", headers=AUTH).json()["seeds"][0]
    assert out["seed"] == "cmt" and out["created"] == 14
    read = {"Authorization": "Bearer r"}
    assert client.post("/jobs/seed", headers=read).status_code == 401  # the read token can't seed
    listed = client.get("/jobs/instruments", params={"curve": "UST"}, headers=read).json()["instruments"]
    assert len(listed) == 14
    one = client.get("/jobs/instruments/UST-10Y-CMT", headers=read).json()
    assert client.get(f"/jobs/instruments/{one['sec_id']}", headers=read).json()["short_name"] == "UST-10Y-CMT"
    assert client.get("/jobs/instruments/NOPE", headers=read).status_code == 404
    r = client.get("/jobs/resolve", params={"scheme": "FRED", "value": ["DGS10", "DGS99"]}, headers=read).json()
    assert [m["short_name"] for m in r["matches"]] == ["UST-10Y-CMT"] and r["unknown"] == ["DGS99"]
    assert client.get("/jobs/search", params={"q": "30-year"}, headers=read).json()["instruments"][0]["short_name"] == "UST-30Y-CMT"


def test_metrics(migrated_db, monkeypatch):
    _tokens(monkeypatch)
    client.post("/jobs/seed", headers=AUTH)
    body = client.get("/metrics").text
    assert "secmaster_svc_up 1" in body
    assert 'secmaster_svc_instruments{type="cmt_yield",curve="UST",status="active"} 14' in body
    assert 'secmaster_svc_identifiers{scheme="H15-TCM"} 11' in body
    assert 'secmaster_svc_seed_ok{seed="cmt"} 1' in body
    assert "secmaster_svc_instruments_without_short_name 0" in body
    assert 'secmaster_svc_seed_last_changed{seed="cmt"} 1' in body


def test_metrics_without_a_database():
    assert "secmaster_svc_up 0" in client.get("/metrics").text
