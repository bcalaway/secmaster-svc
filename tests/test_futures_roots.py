"""Root search for the futures candidates (app/futures_roots.py), against a fake OpenFIGI search."""

from app import figi, futures_roots


def fut(ticker, name, exch="CME", sector="Curncy", figi_="BBG000000001"):
    return {"figi": figi_, "ticker": ticker, "name": name, "exchCode": exch, "marketSector": sector,
            "securityType2": "Future"}


def test_candidates_load():
    got = futures_roots.load()
    codes = [c.cme_code for c in got]
    assert len(codes) == len(set(codes)) == 57 and "RP" in codes and "MTN" in codes and "KRW" in codes
    assert next(c for c in got if c.cme_code == "KRW").kind == "fx_cash"
    rp = next(c for c in got if c.cme_code == "RP")
    assert rp.name == "Euro/British Pound Futures" and rp.exch_code == "CME" and rp.queries


def test_group_by_root_and_name():
    rows = [fut("RPZ6", "EURGBP Crncy Fut  Dec26", figi_="A"), fut("RPH7", "EURGBP Crncy Fut  Mar27", figi_="B"),
            fut("XYZ6", "EUR/GBP ICE       Dec26", exch="ICE", figi_="C"), fut("T 4 01/01/30", "not a future"),
            fut("AEZ10", "EUR/GBP AON       Dec10", figi_="D"), fut("AEH11", "EUR/GBP AON       Mar11", figi_="E"),
            fut("AEM11", "EUR/GBP AON       Jun11", figi_="F")]
    got = futures_roots.group(rows, "CME")
    # The product still listed comes first, though the retired one has more contracts in the answer.
    assert got[0] == {"root": "RP", "name": "EURGBP Crncy Fut", "contracts": 2, "latest": [2027, 3],
                      "example": "RPH7 Curncy (EURGBP Crncy Fut  Mar27)"}
    assert got[1]["root"] == "AE" and got[1]["contracts"] == 3 and got[1]["latest"] == [2011, 6]


def test_run_searches_each_query_and_counts_a_future_once():
    calls = []

    def searcher(body, api_key, pages):
        calls.append(body)
        return [fut("RPZ6", "EURGBP Crncy Fut  Dec26", figi_="A"), fut("RPH7", "EURGBP Crncy Fut  Mar27", figi_="B")]

    out = futures_roots.run("k", only=["RP"], searcher=searcher)
    rp = out["candidates"]["RP"]
    assert out["searched"] == len(calls) == 4 and calls[0]["marketSecDes"] == "Curncy"
    assert calls[0]["securityType2"] == "Future" and calls[0]["exchCode"] == "CME"
    assert rp["groups"][0]["contracts"] == 2 and all(q.endswith(": 2") for q in rp["queries"])


def test_search_pages_and_spacing():
    class Resp:
        def __init__(self, body):
            self.status_code, self._b, self.headers, self.text = 200, body, {}, str(body)

        def json(self):
            return self._b

    pages = [Resp({"data": [{"figi": "A"}], "next": "n1"}), Resp({"data": [{"figi": "B"}]})]
    sent, slept = [], []

    def post(url, json, headers, timeout):
        sent.append(json)
        return pages.pop(0)

    got = figi.search({"query": "EURGBP"}, "k", pages=3, post=post, sleep=slept.append)
    assert [r["figi"] for r in got] == ["A", "B"] and sent[1]["start"] == "n1" and slept == [3.1, 3.1]


def test_job(monkeypatch):
    from fastapi.testclient import TestClient

    from app import jobs
    from app.config import Settings
    from app.main import app

    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t", openfigi_api_key="k"))
    monkeypatch.setattr(figi, "search", lambda body, key, pages: [fut("RPZ6", "EURGBP Crncy Fut  Dec26")])
    monkeypatch.setattr(futures_roots, "PAGES", 1)
    client = TestClient(app)
    out = client.post("/jobs/futures-roots", json={"only": ["RP", "RY"]}, headers={"Authorization": "Bearer t"})
    assert out.status_code == 200 and set(out.json()["candidates"]) == {"RP", "RY"}


def test_a_two_digit_year_from_the_last_century():
    assert futures_roots._month("EURO/GBP FUTURE   Dec99") == (1999, 12)
    assert futures_roots._month("EURGBP Crncy Fut  Mar27") == (2027, 3)
