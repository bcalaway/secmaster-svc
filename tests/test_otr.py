"""On-the-run aliases (app/otr.py)."""

from datetime import date

from app.otr import Auctioned, Interval, alias_name, intervals

TODAY = date(2026, 10, 7)


def _a(sec_id, term, auction, issue, maturity, kind="note"):
    return Auctioned(sec_id, kind, term, date.fromisoformat(auction), date.fromisoformat(issue),
                     date.fromisoformat(maturity))


def test_alias_names():
    assert alias_name("note", "10-Year") == "UST-10Y-OTR" and alias_name("bond", "30-Year") == "UST-30Y-OTR"
    assert alias_name("tips", "5-Year") == "UST-5Y-TII-OTR" and alias_name("frn", "2-Year") == "UST-2Y-FRN-OTR"
    assert alias_name("bill", "13-Week") == "UST-13W-OTR"
    assert alias_name("note", "10-Year", issued=True) == "UST-10Y-OTR-ISSUED"
    assert alias_name("note", None) is None


def test_ten_year_with_reopenings():
    """Feb original, Mar and Apr reopenings, May's new issue: one switch, in May."""
    got = intervals([
        _a(1, "10-Year", "2026-02-11", "2026-02-17", "2036-02-15"),
        _a(1, "10-Year", "2026-03-10", "2026-03-16", "2036-02-15"),
        _a(1, "10-Year", "2026-04-08", "2026-04-15", "2036-02-15"),
        _a(2, "10-Year", "2026-05-06", "2026-05-15", "2036-05-15"),
    ], TODAY)
    by = {(i.alias, i.sec_id): i for i in got}
    assert by[("UST-10Y-OTR", 1)] == Interval("UST-10Y-OTR", 1, date(2026, 2, 11), date(2026, 5, 5))
    assert by[("UST-10Y-OTR", 2)] == Interval("UST-10Y-OTR", 2, date(2026, 5, 6), None)
    # The issued variant switches on the issue date.
    assert by[("UST-10Y-OTR-ISSUED", 1)].valid_to == date(2026, 5, 14)
    assert by[("UST-10Y-OTR-ISSUED", 2)].valid_from == date(2026, 5, 15)


def test_bills_follow_the_program_auctioned():
    """A 13-week auction reopens a 26-week bill: it's the 13-week on-the-run, the 26-week's stays."""
    got = intervals([
        _a(10, "26-Week", "2026-04-06", "2026-04-09", "2026-10-08", "bill"),
        _a(10, "13-Week", "2026-07-06", "2026-07-09", "2026-10-08", "bill"),
        _a(11, "26-Week", "2026-07-06", "2026-07-09", "2027-01-07", "bill"),
    ], TODAY)
    current = {i.alias: i.sec_id for i in got if i.valid_to is None or i.valid_to >= TODAY}
    assert current["UST-26W-OTR"] == 11
    # 10 matures on 2026-10-08: the 13-week alias ends the day before, and nothing has replaced it here.
    thirteen = [i for i in got if i.alias == "UST-13W-OTR"]
    assert thirteen == [Interval("UST-13W-OTR", 10, date(2026, 7, 6), None)]


def test_a_program_that_stops():
    """The 30-year's last auction in 2001, the next in 2006: on the run for a year, then none until 2006."""
    got = intervals([
        _a(20, "30-Year", "2001-08-08", "2001-08-15", "2031-02-15", "bond"),
        _a(21, "30-Year", "2006-02-08", "2006-02-15", "2036-02-15", "bond"),
    ], TODAY)
    first = next(i for i in got if i.alias == "UST-30Y-OTR" and i.sec_id == 20)
    second = next(i for i in got if i.alias == "UST-30Y-OTR" and i.sec_id == 21)
    assert first.valid_to == date(2002, 8, 9)  # a year after its auction, not until 2006
    # Nothing follows it in this test, so by 2026 it has gone stale too.
    assert second.valid_from == date(2006, 2, 8) and second.valid_to == date(2007, 2, 9)


def test_stale_after_a_year_when_nothing_follows():
    got = intervals([_a(30, "20-Year", "1986-07-01", "1986-07-02", "2006-05-15", "bond")], TODAY)
    i = next(x for x in got if x.alias == "UST-20Y-OTR")
    assert i.valid_from == date(1986, 7, 1) and i.valid_to == date(1987, 7, 2)


def test_matured_before_superseded():
    got = intervals([_a(40, "52-Week", "2000-02-01", "2000-02-03", "2001-02-01", "bill")], TODAY)
    i = next(x for x in got if x.alias == "UST-52W-OTR")
    assert i.valid_to == date(2001, 1, 31)


def test_same_day_auctions_keep_the_later_issue():
    got = intervals([
        _a(50, "13-Week", "2026-09-28", "2026-10-01", "2026-12-31", "bill"),
        _a(51, "13-Week", "2026-09-28", "2026-10-02", "2027-01-01", "bill"),
    ], TODAY)
    assert [(i.sec_id, i.valid_to) for i in got if i.alias == "UST-13W-OTR"] == [(51, None)]
