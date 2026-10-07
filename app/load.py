"""The load job: Treasury securities from mkt-data's near-raw records (mkt-data's docs/phase-3.md, step 3).

For TreasuryDirect's records (TD-SECURITIES), period by period (a month of
auctions):

1. List the source's periods from mkt-data, each with `latest_capture_id`.
   A period whose id differs from the watermark here (or is new) is re-read;
   the rest are skipped. A revision or a dropped record can only come from a
   newer capture of that period, so it always moves the id. A period mkt-data
   no longer lists is read as empty.
2. Type each record as an auction (app/treasuries.py). One that can't be typed
   goes to `untyped_record` and is skipped; the load carries on.
3. Diff against the period's auctions here: a new one is inserted (its
   security created if the CUSIP is new), a changed one updated, a missing one
   marked removed.
4. Rebuild every security the period touched from all its current auctions:
   terms (a new `security_terms` row only when something changed, the old one
   superseded), the instrument's attributes, its CUSIP and ISIN identifiers
   and its short name.

Each period commits on its own (auctions, securities, watermark), so a full
history stays small in memory and a failure keeps what's done. Afterwards,
every Treasury instrument's status is refreshed against today (active,
matured, called, withdrawn).

Identity is the CUSIP: a security keeps its sec_id across rebuilds, which
re-read every period (`rebuild` clears the watermarks).
"""

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import treasuries as tr
from app.models import (
    Auction,
    Identifier,
    Instrument,
    InstrumentName,
    LoadRun,
    SecurityTerms,
    SourcePeriod,
    UntypedRecord,
)
from app.upstream import Rec, Upstream

SOURCES = ["TD-SECURITIES"]
NEW_YORK = ZoneInfo("America/New_York")
TERM_COLUMNS = [c.name for c in SecurityTerms.__table__.columns
                if c.name not in ("id", "sec_id", "provenance", "checks", "recorded_at", "superseded_at")]
RESULT_COLUMNS = list(tr.RESULTS)
TREASURY_TYPES = sorted({t for t, _ in tr.TYPES.values()})


class LoadError(RuntimeError):
    pass


# --- identity ------------------------------------------------------------------


def sec_id_for(s: Session, cusip: str) -> int | None:
    return s.scalar(select(Identifier.sec_id).where(
        Identifier.scheme == "CUSIP", Identifier.value == cusip, Identifier.removed_at.is_(None)))


def _new_security(s: Session, a: tr.Auction) -> int:
    inst = Instrument(type=a.inst_type, currency="USD", country="US", curve="UST", tenor=None,
                      calendar="SIFMA-US", status="active", description="")
    s.add(inst)
    s.flush()
    s.add(Identifier(sec_id=inst.sec_id, scheme="CUSIP", value=a.cusip))
    s.flush()
    return inst.sec_id


def _ensure_identifier(s: Session, sec_id: int, scheme: str, value: str) -> None:
    row = s.scalar(select(Identifier).where(Identifier.scheme == scheme, Identifier.value == value,
                                            Identifier.removed_at.is_(None)))
    if row is None:
        s.add(Identifier(sec_id=sec_id, scheme=scheme, value=value))
    elif row.sec_id != sec_id:
        raise LoadError(f"{scheme} {value} already belongs to sec_id {row.sec_id}, not {sec_id}")


def _set_short_name(s: Session, sec_id: int, wanted: str, cusip: str, now: datetime) -> str:
    """Give the security its short name; the old one stays as an alias. A name another security
    already has (two bills maturing the same day) gets the CUSIP appended."""
    owner = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == wanted))
    if owner is not None and owner != sec_id:
        wanted = f"{wanted}-{cusip}"
    rows = {r.name: r for r in s.scalars(select(InstrumentName).where(InstrumentName.sec_id == sec_id))}
    current = next((r for r in rows.values() if r.kind == "short" and r.removed_at is None), None)
    if current is not None and current.name == wanted:
        return wanted
    if current is not None:
        current.kind = "alias"
        s.flush()
    if wanted in rows:
        rows[wanted].kind, rows[wanted].removed_at = "short", None
    else:
        s.add(InstrumentName(sec_id=sec_id, name=wanted, kind="short", created_at=now))
    s.flush()
    return wanted


# --- one security ----------------------------------------------------------------


def _typed(a: Auction) -> tr.Auction:
    return tr.type_auction(a.source_key, a.fields)


def _status(t: dict | None, today: date, has_auctions: bool) -> str:
    if not has_auctions or t is None:
        return "withdrawn"
    if t["called_date"] and t["called_date"] <= today:
        return "called"
    if t["maturity_date"] < today:
        return "matured"
    return "active"


def rebuild_security(s: Session, sec_id: int, now: datetime, today: date) -> dict:
    """Terms, attributes, identifiers and name of one security, from its current auctions."""
    rows = list(s.scalars(select(Auction).where(Auction.sec_id == sec_id, Auction.removed_at.is_(None))))
    inst = s.get(Instrument, sec_id)
    current = s.scalar(select(SecurityTerms).where(SecurityTerms.sec_id == sec_id,
                                                   SecurityTerms.superseded_at.is_(None)))
    if not rows:
        inst.status, inst.updated_at = "withdrawn", now
        return {"terms_recorded": 0}
    terms = tr.build_terms([_typed(a) for a in rows])
    v = terms.values
    wanted = {c: v.get(c) for c in TERM_COLUMNS}
    recorded = 0
    if current is None or any(getattr(current, c) != wanted[c] for c in TERM_COLUMNS) \
            or current.provenance != terms.provenance or current.checks != terms.checks:
        if current is not None:
            current.superseded_at = now
            s.flush()
        s.add(SecurityTerms(sec_id=sec_id, **wanted, provenance=terms.provenance, checks=terms.checks,
                            recorded_at=now))
        recorded = 1
    tenor = tr.term_iso(v["term"])
    attrs = {"type": v["inst_type"], "currency": "USD", "country": "US", "curve": "UST", "tenor": tenor,
             "calendar": v["calendar"], "description": tr.describe(v), "status": _status(v, today, True)}
    if any(getattr(inst, k) != val for k, val in attrs.items()):
        for k, val in attrs.items():
            setattr(inst, k, val)
        inst.updated_at = now
    _ensure_identifier(s, sec_id, "CUSIP", v["cusip"])
    _ensure_identifier(s, sec_id, "ISIN", tr.isin(v["cusip"]))
    _set_short_name(s, sec_id, tr.short_name(v), v["cusip"], now)
    s.flush()
    return {"terms_recorded": recorded}


# --- one period --------------------------------------------------------------------


def _untyped(s: Session, source: str, period: str, key: str, problem: str, now: datetime) -> None:
    row = s.get(UntypedRecord, (source, key))
    if row is None:
        s.add(UntypedRecord(source=source, source_key=key, period=period, problem=problem[:2000],
                            first_seen=now, last_seen=now))
    else:
        row.period, row.problem, row.last_seen = period, problem[:2000], now


def apply_period(s: Session, source: str, period: str, recs: list[Rec], now: datetime, today: date) -> dict:
    """One period's records into auctions, then every touched security rebuilt. Flushes, doesn't commit."""
    have = {a.source_key: a for a in s.scalars(select(Auction).where(Auction.source == source,
                                                                     Auction.period == period))}
    out = {"records": len(recs), "added": 0, "updated": 0, "removed": 0, "untyped": 0, "created": 0,
           "terms_recorded": 0}
    touched: set[int] = set()
    seen: set[str] = set()
    for r in recs:
        if r.record_type != "auction":
            continue
        try:
            a = tr.type_auction(r.source_key, r.fields)
        except tr.Untypable as e:
            _untyped(s, source, period, r.source_key, str(e), now)
            out["untyped"] += 1
            continue
        old_untyped = s.get(UntypedRecord, (source, r.source_key))
        if old_untyped is not None:
            s.delete(old_untyped)
        seen.add(r.source_key)
        row = have.get(r.source_key)
        typed = {"cusip": a.cusip, "security_type": a.security_type, "reopening": a.reopening, "term": a.term,
                 "security_term": a.security_term, "announcement_date": a.announcement_date,
                 "auction_date": a.auction_date, "issue_date": a.issue_date, **a.results}
        if row is None:
            sec_id = sec_id_for(s, a.cusip)
            if sec_id is None:
                sec_id = _new_security(s, a)
                out["created"] += 1
            s.add(Auction(sec_id=sec_id, source=source, period=period, source_key=r.source_key, **typed,
                          fields=r.fields, record_id=r.record_id, capture_id=r.capture_id, loaded_at=now,
                          updated_at=now))
            out["added"] += 1
            touched.add(sec_id)
        else:
            changed = row.removed_at is not None or row.fields != r.fields \
                or any(getattr(row, k) != val for k, val in typed.items())
            if changed:
                for k, val in typed.items():
                    setattr(row, k, val)
                row.fields, row.removed_at, row.updated_at = r.fields, None, now
                out["updated"] += 1
                touched.add(row.sec_id)
            row.record_id, row.capture_id = r.record_id, r.capture_id
    for key, row in have.items():
        if key not in seen and row.removed_at is None:
            row.removed_at, row.updated_at = now, now
            out["removed"] += 1
            touched.add(row.sec_id)
    s.flush()
    for sec_id in sorted(touched):
        out["terms_recorded"] += rebuild_security(s, sec_id, now, today)["terms_recorded"]
    out["securities_touched"] = len(touched)
    return out


# --- the whole load ------------------------------------------------------------------


def refresh_status(s: Session, today: date, now: datetime) -> dict:
    """Active, matured, called or withdrawn, for every Treasury instrument, as of today."""
    terms = {t.sec_id: t for t in s.scalars(select(SecurityTerms).where(SecurityTerms.superseded_at.is_(None)))}
    live = set(s.scalars(select(Auction.sec_id).where(Auction.removed_at.is_(None)).distinct()))
    counts: dict[str, int] = {}
    for inst in s.scalars(select(Instrument).where(Instrument.type.in_(TREASURY_TYPES))):
        t = terms.get(inst.sec_id)
        status = _status({"called_date": t.called_date, "maturity_date": t.maturity_date} if t else None,
                         today, inst.sec_id in live)
        if inst.status != status:
            inst.status, inst.updated_at = status, now
        counts[status] = counts.get(status, 0) + 1
    return counts


def _load(s: Session, up: Upstream, now: datetime, today: date, progress: dict) -> None:
    for source in SOURCES:
        marks = dict(s.execute(select(SourcePeriod.period, SourcePeriod.capture_id)
                               .where(SourcePeriod.source == source)).all())
        periods = up.list_periods(source)
        listed = {p.period for p in periods}
        todo = [p for p in periods if marks.get(p.period) != p.latest_capture_id]
        gone = sorted(set(marks) - listed)
        progress["periods_skipped"] += len(periods) - len(todo)
        for p in todo:
            result = apply_period(s, source, p.period, up.get_period(source, p.period), now, today)
            mark = s.get(SourcePeriod, (source, p.period))
            if mark is None:
                s.add(SourcePeriod(source=source, period=p.period, capture_id=p.latest_capture_id,
                                   records=p.records, loaded_at=now))
            else:
                mark.capture_id, mark.records, mark.loaded_at = p.latest_capture_id, p.records, now
            s.commit()
            s.expunge_all()
            progress["periods_read"] += 1
            for k in ("added", "updated", "removed", "untyped", "created", "terms_recorded"):
                progress[k] += result[k]
        for period in gone:
            result = apply_period(s, source, period, [], now, today)
            s.execute(delete(SourcePeriod).where(SourcePeriod.source == source, SourcePeriod.period == period))
            s.commit()
            progress["periods_read"] += 1
            progress["removed"] += result["removed"]
    progress["status"] = refresh_status(s, today, now)
    s.commit()


def run(s: Session, up: Upstream, now: datetime | None = None, today: date | None = None) -> dict:
    """Load what's new in mkt-data. Records a load_run either way; re-raises a failure as LoadError."""
    now = now or datetime.now(UTC)
    today = today or now.astimezone(NEW_YORK).date()
    progress = {"periods_read": 0, "periods_skipped": 0, "added": 0, "updated": 0, "removed": 0,
                "untyped": 0, "created": 0, "terms_recorded": 0}
    try:
        _load(s, up, now, today, progress)
    except Exception as e:
        s.rollback()
        s.add(LoadRun(started_at=now, finished_at=datetime.now(UTC), outcome="error",
                      detail=json.dumps({"error": str(e)[:2000], "progress": progress}, default=str)))
        s.commit()
        raise LoadError(f"load failed after {progress['periods_read']} periods: {e}") from e
    progress["securities"] = s.scalar(select(func.count()).select_from(SecurityTerms)
                                      .where(SecurityTerms.superseded_at.is_(None)))
    s.add(LoadRun(started_at=now, finished_at=datetime.now(UTC), outcome="ok", detail=json.dumps(progress)))
    s.commit()
    return progress


def rebuild(s: Session, up: Upstream, now: datetime | None = None, today: date | None = None) -> dict:
    """Re-read every period: watermarks cleared, then a load. sec_ids are kept (identity is the CUSIP)."""
    s.execute(delete(SourcePeriod))
    s.commit()
    return run(s, up, now, today)
