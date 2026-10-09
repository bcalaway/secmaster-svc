"""The fixings seed (seeds/fixings.toml, mkt-data's docs/phase-4.md step 4)."""

from sqlalchemy import select

from app import db, seed
from app.models import Identifier, Instrument, InstrumentName


def _parsed():
    return {i.short_name: i for i in seed.load(seed.SEEDS / "fixings.toml").instruments}


def test_names_say_which_way_each_rate_is_quoted():
    got = _parsed()
    assert dict(got["EURUSD-H10"].identifiers) == {"FRB-H10-RATES": "RXI$US_N.B.EU"}
    assert got["EURUSD-H10"].attrs["currency"] == "USD"  # dollars per euro
    assert dict(got["USDJPY-H10"].identifiers) == {"FRB-H10-RATES": "RXI_N.B.JA"}
    assert got["USDJPY-H10"].attrs["currency"] == "JPY"
    assert dict(got["EURJPY-ECB"].identifiers) == {"ECB-EXR": "EXR.D.JPY.EUR.SP00.A"}
    assert got["EURJPY-ECB"].attrs["calendar"] == "TARGET"
    assert dict(got["SOFR"].identifiers) == {"NYFED-SOFR": "SOFR"} and got["SOFR"].attrs["type"] == "rate_fixing"
    assert dict(got["USD-BROAD-H10"].identifiers) == {"FRB-H10": "JRXWTFB_N.B"}


def test_no_averages():
    assert not [n for n in _parsed() if "AVG" in n or "INDEX" in n]


def test_applies_once(migrated_db):
    with db.session() as s:
        first = {r["seed"]: r for r in seed.apply_all(s)}
        second = {r["seed"]: r for r in seed.apply_all(s)}
        sofr = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == "SOFR"))
        assert s.get(Instrument, sofr).type == "rate_fixing"
        assert s.scalar(select(Identifier.value).where(Identifier.sec_id == sofr, Identifier.scheme == "NYFED-SOFR")) == "SOFR"
    assert first["fixings"]["created"] == 72 and second["fixings"]["created"] == 0
