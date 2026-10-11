"""Check the SPGMI swap curve files' conventions against swap_terms (mkt-data's docs/phase-4.md, "Swap curves").

swap_terms comes from seeds/swaps.toml, which is the spec as written. Each daily file states its own conventions too
(mkt-data keeps them as a `curve` record per file), so this compares the two and reports any difference instead of
trusting either: if S&P changes something, the seed is the one to review.

Per source (SPGMI-RFR-<CCY>), for the latest RECENT files (one per publication date):

- day counts: the file's fixed and floating ones against fixed_day_count and floating_day_count;
- payment frequencies: the file's `1Y` against `P1Y`;
- bad-day convention: the file's `M` against modified_following;
- calendars: the file's `none`, `NYM` or `TYO` against adjust_calendar (none, ISDA-NYM, ISDA-TYO);
- tenors: the file's curve points against the instruments' identifiers, both ways;
- spot date: the file's against the trade date (effectiveasof) plus spot_lag_days weekdays, skipping
  spot_calendar's closed days (calendar-svc);
- snap time: the time of day in the file's snaptime against snap_time.

The result replaces the source's row in `swap_check` (outcome ok, mismatch, or no_files), so the latest state is one
read away and a metric per source can alert. Nothing else changes.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SwapCheck, SwapTerms

RECENT = 10  # files per source checked each run
BAD_DAY = {"M": "modified_following", "F": "following", "P": "preceding", "N": "none"}
ISDA_CALENDARS = {"NYM": "ISDA-NYM", "TYO": "ISDA-TYO"}
MAX_DIFFS = 50  # per source, kept in the row


def _norm(v) -> str:
    return str(v or "").strip().upper()


def spot_date(trade: date, lag: int, closed: set[date]) -> date:
    """The trade date plus `lag` business days (weekdays not in `closed`); lag 0 is the trade date itself."""
    d, n = trade, 0
    while n < lag:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in closed:
            n += 1
    return d


def check_record(fields: dict, terms: SwapTerms, tenors: set[str], closed: set[date]) -> list[dict]:
    """The differences between one file's stated conventions and the seed's, as {field, file, seed}."""
    diffs = []

    def differ(field, file_value, seed_value):
        diffs.append({"field": field, "file": file_value, "seed": seed_value})

    for file_key, attr in (("fixeddaycountconvention", "fixed_day_count"),
                           ("floatingdaycountconvention", "floating_day_count")):
        if _norm(fields.get(file_key)) != _norm(getattr(terms, attr)):
            differ(file_key, fields.get(file_key), getattr(terms, attr))
    for file_key, attr in (("fixedpaymentfrequency", "fixed_frequency"),
                           ("floatingpaymentfrequency", "floating_frequency")):
        if "P" + _norm(fields.get(file_key)) != _norm(getattr(terms, attr)):
            differ(file_key, fields.get(file_key), getattr(terms, attr))
    bad_day = BAD_DAY.get(_norm(fields.get("baddayconvention")), fields.get("baddayconvention"))
    if bad_day != terms.business_day_convention:
        differ("baddayconvention", fields.get("baddayconvention"), terms.business_day_convention)
    stated = sorted(ISDA_CALENDARS.get(_norm(c), c) for c in fields.get("calendars") or [] if _norm(c) != "NONE")
    expected = [terms.adjust_calendar] if terms.adjust_calendar else []
    if stated != expected:
        differ("calendars", fields.get("calendars"), terms.adjust_calendar or "none")
    in_file = {p.get("tenor") for p in fields.get("curvepoints") or []}
    if in_file != tenors:
        differ("tenors", sorted(in_file - tenors), sorted(tenors - in_file))  # (only in the file, only in the seed)
    try:
        trade = date.fromisoformat(str(fields.get("effectiveasof"))[:10])
        want = spot_date(trade, terms.spot_lag_days, closed)
        if str(fields.get("spotdate"))[:10] != want.isoformat():
            differ("spotdate", fields.get("spotdate"), want.isoformat())
    except ValueError:
        differ("effectiveasof", fields.get("effectiveasof"), "a date")
    snap = str(fields.get("snaptime") or "")
    if snap[11:16] != terms.snap_time:
        differ("snaptime", snap, terms.snap_time)
    return diffs


def run(s: Session, up, cals, now: datetime | None = None) -> dict:
    """Check each swap curve source's recent files. `up` is mkt-data (records), `cals` calendar-svc. Commits."""
    now = now or datetime.now(UTC)
    rows = s.scalars(select(SwapTerms).where(SwapTerms.superseded_at.is_(None))).all()
    by_source: dict[str, list[SwapTerms]] = defaultdict(list)
    for t in rows:
        by_source[t.source].append(t)
    summary = {}
    for source, terms_rows in sorted(by_source.items()):
        terms = terms_rows[0]  # every row of a curve carries the same conventions; tenor-specific: coupon only
        tenors = {t.source_key for t in terms_rows}
        periods = sorted(up.list_periods(source), key=lambda p: p.period)[-RECENT:]
        diffs, checked = [], []
        if periods:
            closed: set[date] = set()
            if terms.spot_calendar:
                first = date.fromisoformat(periods[0].period) - timedelta(days=7)
                last = date.fromisoformat(periods[-1].period) + timedelta(days=21)
                closed = set(cals.closed(terms.spot_calendar, first, last))
            for p in periods:
                for rec in up.get_period(source, p.period):
                    if rec.record_type != "curve":
                        continue
                    checked.append(p.period)
                    diffs += [{"period": p.period, **d} for d in check_record(rec.fields, terms, tenors, closed)]
        outcome = "no_files" if not checked else ("mismatch" if diffs else "ok")
        row = s.get(SwapCheck, source) or SwapCheck(source=source)
        row.checked_at, row.outcome, row.files = now, outcome, len(checked)
        row.last_period = checked[-1] if checked else None
        row.detail = diffs[:MAX_DIFFS]
        s.add(row)
        summary[source] = {"outcome": outcome, "files": len(checked), "last": row.last_period,
                           "differences": len(diffs), "first": diffs[:3]}
    s.commit()
    return {"sources": summary, "mismatches": sorted(k for k, v in summary.items() if v["outcome"] == "mismatch")}
