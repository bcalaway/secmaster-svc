"""OpenFIGI mapping (app/figi.py) and the FIGI job (app/figi_job.py), against a fake OpenFIGI."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import db, figi, figi_job, load, securities
from app.models import FigiLookup, Identifier
from tests.test_load import NOW, TODAY, FakeMktData, _records

FOUND = {"data": [{"figi": "BBG01ABCDEF0", "compositeFIGI": None, "ticker": "T 3 3/4 02/28/33",
                   "marketSector": "Govt", "securityType": "US GOVERNMENT"}]}
MISSING = {"warning": "No identifier found."}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, str(body)

    def json(self):
        return self._body


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_parse():
    got = figi.parse(["91282CQC8", "912810XX0", "912828ZZ9"], [FOUND, MISSING, {"error": "Invalid idValue format"}])
    assert [a.outcome for a in got] == ["found", "not_found", "error"]
    assert got[0].figi == "BBG01ABCDEF0" and got[0].ticker == "T 3 3/4 02/28/33"
    with pytest.raises(figi.FigiError):
        figi.parse(["a", "b"], [FOUND])


def test_batches_and_the_rate_limit_with_a_key():
    calls, clock = [], Clock()

    def post(url, json, headers, timeout):
        calls.append((len(json), headers.get("X-OPENFIGI-APIKEY")))
        clock.t += 0.01
        return Resp(200, [MISSING] * len(json))

    cusips = [f"{i:09d}" for i in range(2_600)]  # 26 requests of 100: the 26th waits for the window
    out = figi.map_cusips(cusips, "k", post=post, sleep=clock.sleep, clock=clock)
    assert len(out) == 2_600 and len(calls) == 26 and {n for n, _ in calls} == {100} and calls[0][1] == "k"
    assert len(clock.slept) == 1 and 5.5 < clock.slept[0] < 6.2


def test_without_a_key_batches_of_ten():
    sizes = []

    def post(url, json, headers, timeout):
        sizes.append(len(json))
        assert "X-OPENFIGI-APIKEY" not in headers
        return Resp(200, [MISSING] * len(json))

    clock = Clock()
    figi.map_cusips([f"{i:09d}" for i in range(25)], None, post=post, sleep=clock.sleep, clock=clock)
    assert sizes == [10, 10, 5]


def test_429_waits_and_retries():
    answers = [Resp(429, {}, {"ratelimit-reset": "3"}), Resp(200, [FOUND])]
    clock = Clock()
    out = figi.map_cusips(["91282CQC8"], "k", post=lambda *a, **k: answers.pop(0), sleep=clock.sleep, clock=clock)
    assert out[0].outcome == "found" and clock.slept == [4.0]
    with pytest.raises(figi.FigiError, match="HTTP 500"):
        figi.map_cusips(["91282CQC8"], "k", post=lambda *a, **k: Resp(500, {}), sleep=clock.sleep, clock=clock)


def test_due():
    assert figi.due(None, None, NOW)
    assert not figi.due(NOW, "found", NOW + timedelta(days=400))
    assert not figi.due(NOW, "not_found", NOW + timedelta(days=10))
    assert figi.due(NOW, "not_found", NOW + timedelta(days=31))


def test_job(migrated_db):
    mkt = FakeMktData()
    mkt.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    with db.session() as s:
        load.run(s, mkt, NOW, TODAY)
    asked = []

    def mapper(cusips, key):
        asked.append(list(cusips))
        return [figi.Answer(c, "found", f"BBG{c}", None, "SP 0 02/15/56" if c.startswith("912803") else f"T {c}",
                            {"data": []}) if c != "912797TB3" else figi.Answer(c, "not_found", detail=MISSING)
                for c in cusips]

    with db.session() as s:
        out = figi_job.run(s, "k", NOW, mapper)
        assert out["asked"] == len(asked[0]) and out["not_found"] == 1 and out["found"] == out["asked"] - 1
        note = securities.get(s, name="UST-3.75-2033-02-28")
        assert {i["scheme"]: i["value"] for i in note["identifiers"]}["FIGI"] == "BBG91282CQC8"
        assert s.get(FigiLookup, "912797TB3").outcome == "not_found"
        # Principal STRIPS sharing a ticker: the first keeps it, the rest are listed.
        tickers = s.scalars(select(Identifier.value).where(Identifier.scheme == "TICKER")).all()
        assert tickers.count("SP 0 02/15/56") == 1
        # Asked once: a second run has nothing to do, until a month has passed for the one not found.
        assert figi_job.run(s, "k", NOW, mapper)["asked"] == 0
        again = figi_job.run(s, "k", NOW + timedelta(days=31), mapper)
        assert again["asked"] == 1 and asked[-1] == ["912797TB3"]


def test_job_without_a_key_does_a_few_hundred(migrated_db, monkeypatch):
    mkt = FakeMktData()
    mkt.put("2026-02", _records("td_securities_2026_02_capture1319.json"))
    with db.session() as s:
        load.run(s, mkt, NOW, TODAY)
    monkeypatch.setattr(figi, "MAX_WITHOUT_KEY", 5)
    with db.session() as s:
        out = figi_job.run(s, None, NOW, lambda cs, k: [figi.Answer(c, "not_found") for c in cs])
    assert out["asked"] == 5 and out["left"] > 0 and out["with_key"] is False
