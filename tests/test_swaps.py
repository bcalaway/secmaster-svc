"""The OIS swap seed (seeds/swaps.toml, app/swaps.py; mkt-data's docs/phase-4.md, "Swap curves").

The expected conventions are the SPGMI Interest Rate Curve XML Specification (RFRs), v1.3, sections 3.1 and 3.2.1-3.2.2.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app import db, seed, swaps
from app.models import Identifier, Instrument, InstrumentName, SwapTerms


def _swaps():
    return {w.instrument.short_name: w for w in swaps.load()[1]}


def test_tenors_per_currency():
    got = _swaps()
    by_ccy = {}
    for w in got.values():
        by_ccy.setdefault(w.instrument.attrs["currency"], []).append(w.terms["source_key"])
    full = ["1M", "2M", "3M", "6M", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "12Y", "15Y", "20Y",
            "25Y", "30Y"]
    assert by_ccy["USD"] == full and by_ccy["GBP"] == full and by_ccy["CHF"] == full and by_ccy["AUD"] == full
    assert by_ccy["EUR"] == [t for t in full if t not in ("2M", "25Y")]  # no 2M or 25Y for EUR
    assert by_ccy["JPY"] == [t for t in full if t != "25Y"]  # no 25Y for JPY
    assert not [n for n in got if n.endswith("-9M")]  # nobody publishes 9M
    assert len(got) == 19 * 4 + 17 + 18


def test_instruments():
    w = _swaps()["USD-SOFR-OIS-10Y"]
    a = w.instrument.attrs
    assert (a["type"], a["currency"], a["curve"], a["tenor"], a["calendar"]) == (
        "swap_ois", "USD", "ISDA-RFR-USD", "P10Y", "FED")
    assert dict(w.instrument.identifiers) == {"SPGMI-RFR-USD": "10Y"}
    assert "EUR-ESTR-OIS-5Y" in _swaps() and "JPY-TONA-OIS-30Y" in _swaps()


@pytest.mark.parametrize(("name", "dcc", "lag", "spot_cal", "adjust_cal"), [
    ("USD-SOFR-OIS-5Y", "ACT/360", 2, "ISDA-NYM", "ISDA-NYM"),
    ("EUR-ESTR-OIS-5Y", "ACT/360", 2, None, None),
    ("GBP-SONIA-OIS-5Y", "ACT/365", 0, None, None),
    ("JPY-TONA-OIS-5Y", "ACT/365", 2, "ISDA-TYO", "ISDA-TYO"),
    ("CHF-SARON-OIS-5Y", "ACT/360", 2, None, None),
    ("AUD-AONIA-OIS-5Y", "ACT/365", 1, None, None),
])
def test_conventions(name, dcc, lag, spot_cal, adjust_cal):
    t = _swaps()[name].terms
    assert (t["money_market_day_count"], t["fixed_day_count"], t["floating_day_count"]) == (dcc, dcc, dcc)
    assert (t["spot_lag_days"], t["spot_calendar"], t["adjust_calendar"]) == (lag, spot_cal, adjust_cal)
    assert (t["fixed_frequency"], t["floating_frequency"], t["business_day_convention"]) == (
        "P1Y", "P1Y", "modified_following")
    assert (t["model_instrument_type"], t["snap_time"], t["publication_deadline"]) == ("S", "16:00", "17:30")
    assert t["cite"].startswith("SPGMI Interest Rate Curve XML Specification (RFRs), v1.3")


def test_zero_coupon_up_to_one_year():
    got = _swaps()
    assert [got[f"USD-SOFR-OIS-{k}"].terms["coupon"] for k in ("1M", "6M", "1Y", "2Y", "30Y")] == [
        "zero", "zero", "zero", "periodic", "periodic"]


def test_bad_seeds():
    good = (seed.SEEDS / "swaps.toml").read_text()
    for broken, match in [
        (good.replace('fixed_day_count = "ACT/360"', 'fixed_day_count = "30/360"', 1), "fixed_day_count"),
        (good.replace("spot_lag_days = 2", "spot_lag_days = 9", 1), "spot_lag_days"),
        (good.replace('"1M", "2M"', '"1M", "1M"', 1), "once each"),
        (good.replace('snap_time = "16:00"', 'snap_time = "4pm"'), "HH:MM"),
        (good.replace('index = "SOFR"\n', ""), "missing index"),
    ]:
        with pytest.raises(seed.SeedError, match=match):
            swaps.parse(broken.encode())


def test_applies_once_and_supersedes_a_changed_convention(migrated_db):
    with db.session() as s:
        first = {r["seed"]: r for r in seed.apply_all(s)}
        second = {r["seed"]: r for r in seed.apply_all(s)}
        sec = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == "USD-SOFR-OIS-10Y"))
        assert s.get(Instrument, sec).type == "swap_ois"
        assert s.scalar(select(Identifier.value).where(Identifier.sec_id == sec,
                                                       Identifier.scheme == "SPGMI-RFR-USD")) == "10Y"
        row = s.scalars(select(SwapTerms).where(SwapTerms.sec_id == sec)).one()
        assert (row.fixed_day_count, row.spot_calendar, row.coupon, row.timezone) == (
            "ACT/360", "ISDA-NYM", "periodic", "America/New_York")
    n = 19 * 4 + 17 + 18
    assert first["swaps"]["created"] == n and first["swaps"]["terms_added"] == n
    assert second["swaps"]["created"] == 0 and second["swaps"]["terms_added"] == 0

    changed = (seed.SEEDS / "swaps.toml").read_text().replace('publication_time = "16:50"', 'publication_time = "16:55"')
    with db.session() as s:
        out = swaps.apply(s, swaps.parse(changed.encode()), datetime(2026, 10, 11, tzinfo=UTC))
        assert out["terms_superseded"] == n and out["terms_added"] == n
        assert s.scalar(select(func.count()).select_from(SwapTerms)) == 2 * n
        assert s.scalar(select(func.count()).select_from(SwapTerms).where(SwapTerms.superseded_at.is_(None))) == n
