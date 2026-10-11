"""The swap curve conventions check (app/swap_check.py)."""

from datetime import UTC, date, datetime

from sqlalchemy import select

from app import db, seed, swap_check
from app.models import SwapCheck
from app.upstream import Period, Rec

NOW = datetime(2026, 10, 11, tzinfo=UTC)
USD_TENORS = ["1M", "2M", "3M", "6M", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "12Y", "15Y", "20Y",
              "25Y", "30Y"]


def curve(effective="2026-10-12", spot="2026-10-14", snap="2026-10-09T16:00:00", calendars=("NYM",), dcc="ACT/360",
          tenors=USD_TENORS, bad_day="M"):
    return {"effectiveasof": effective, "currency": "USD", "baddayconvention": bad_day,
            "fixeddaycountconvention": dcc, "floatingdaycountconvention": dcc, "fixedpaymentfrequency": "1Y",
            "floatingpaymentfrequency": "1Y", "snaptime": snap, "spotdate": spot, "calendars": list(calendars),
            "curvepoints": [{"tenor": t, "maturitydate": "2030-01-01", "parrate": "0.03"} for t in tenors]}


class FakeUp:
    def __init__(self, files):  # source -> {period: fields}
        self.files = files

    def list_periods(self, source):
        return [Period(p, i + 1, 1) for i, p in enumerate(sorted(self.files.get(source, {})))]

    def get_period(self, source, period):
        return [Rec(1, "curve", period, period, self.files[source][period], 1)]


class FakeCals:
    def __init__(self, closed=()):
        self.days = set(closed)

    def closed(self, calendar, start, end):
        return {d for d in self.days if start <= d <= end}


def _run(files, closed=()):
    with db.session() as s:
        seed.apply_all(s)
        out = swap_check.run(s, FakeUp(files), FakeCals(closed), NOW)
        rows = {r.source: r for r in s.scalars(select(SwapCheck))}
        return out, rows


def test_spot_date():
    assert swap_check.spot_date(date(2026, 10, 9), 2, set()) == date(2026, 10, 13)  # Friday + 2 weekdays
    assert swap_check.spot_date(date(2026, 10, 9), 2, {date(2026, 10, 12)}) == date(2026, 10, 14)
    assert swap_check.spot_date(date(2026, 10, 12), 0, {date(2026, 10, 12)}) == date(2026, 10, 12)  # GBP: T


def test_a_file_that_agrees(migrated_db):
    out, rows = _run({"SPGMI-RFR-USD": {"2026-10-09": curve()}})
    assert out["sources"]["SPGMI-RFR-USD"]["outcome"] == "ok" and out["mismatches"] == []
    assert rows["SPGMI-RFR-USD"].files == 1 and rows["SPGMI-RFR-USD"].last_period == "2026-10-09"
    assert rows["SPGMI-RFR-EUR"].outcome == "no_files"


def test_spot_skips_an_isda_holiday(migrated_db):
    # Trade date Friday 2026-06-19; Monday 2026-06-22 an ISDA-NYM holiday (made up for the test): spot is Wednesday.
    files = {"SPGMI-RFR-USD": {"2026-06-18": curve(effective="2026-06-19", spot="2026-06-24",
                                                    snap="2026-06-18T16:00:00")}}
    out, _ = _run(files, closed={date(2026, 6, 22)})
    assert out["sources"]["SPGMI-RFR-USD"]["outcome"] == "ok"


def test_differences_are_reported(migrated_db):
    bad = curve(dcc="ACT/365", calendars=("none",), tenors=USD_TENORS[:-1] + ["40Y"], spot="2026-10-13",
                snap="2026-10-09T17:00:00", bad_day="F")
    out, rows = _run({"SPGMI-RFR-USD": {"2026-10-09": bad}})
    assert out["mismatches"] == ["SPGMI-RFR-USD"]
    fields = {d["field"]: d for d in rows["SPGMI-RFR-USD"].detail}
    assert set(fields) == {"fixeddaycountconvention", "floatingdaycountconvention", "calendars", "tenors", "spotdate",
                           "snaptime", "baddayconvention"}
    assert fields["tenors"]["file"] == ["40Y"] and fields["tenors"]["seed"] == ["30Y"]
    assert fields["spotdate"]["seed"] == "2026-10-14"
