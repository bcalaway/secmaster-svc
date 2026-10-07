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
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import otr, strips
from app import treasuries as tr
from app.models import (
    Auction,
    Identifier,
    Instrument,
    InstrumentName,
    LoadRun,
    SecurityTerms,
    SourcePeriod,
    Strip,
    StrippedAmount,
    UntypedRecord,
)
from app.upstream import Rec, Upstream

SOURCES = ["TD-SECURITIES", "FD-MSPD-STRIPS"]
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


def sync_otr(s: Session, today: date, now: datetime) -> dict:
    """Recompute the on-the-run aliases from every current auction and bring `identifier` (scheme OTR) in line.

    An interval that's still right is left alone; one that changed (a new
    auction ended it, or it went stale) is replaced: the old row gets
    removed_at, the new one is added.
    """
    maturity = dict(s.execute(select(SecurityTerms.sec_id, SecurityTerms.maturity_date)
                              .where(SecurityTerms.superseded_at.is_(None))).all())
    rows = s.execute(select(Auction.sec_id, Auction.security_type, Auction.term, Auction.auction_date,
                            Auction.issue_date, Auction.fields["type"].as_string(),
                            Auction.fields["cashManagementBillCMB"].as_string())
                     .where(Auction.removed_at.is_(None))).all()
    auctions = [otr.Auctioned(sec_id, sec_type, term, a_date, i_date, maturity[sec_id])
                for sec_id, sec_type, term, a_date, i_date, td_type, cmb in rows
                if sec_id in maturity and td_type != "CMB" and cmb != "Yes"]
    want = {(i.alias, i.valid_from): i for i in otr.intervals(auctions, today)}
    have = {(r.value, r.valid_from): r for r in s.scalars(select(Identifier).where(
        Identifier.scheme == otr.SCHEME, Identifier.removed_at.is_(None)))}
    out = {"otr_added": 0, "otr_removed": 0}
    for key, row in have.items():
        i = want.get(key)
        if i is None or (row.sec_id, row.valid_to) != (i.sec_id, i.valid_to):
            row.removed_at = now
            out["otr_removed"] += 1
    s.flush()
    for key, i in want.items():
        row = have.get(key)
        if row is None or row.removed_at is not None:
            s.add(Identifier(sec_id=i.sec_id, scheme=otr.SCHEME, value=i.alias, valid_from=i.valid_from,
                             valid_to=i.valid_to, created_at=now))
            out["otr_added"] += 1
    s.flush()
    out["otr_current"] = sum(1 for i in want.values() if i.valid_to is None)
    return out


def _typing_version(s: Session) -> int | None:
    """The typing version the last successful load ran with (none before version 2 recorded it)."""
    for detail in s.scalars(select(LoadRun.detail).where(LoadRun.outcome == "ok").order_by(LoadRun.id.desc()).limit(1)):
        return json.loads(detail).get("typing_version")
    return None


def retype_all(s: Session, now: datetime, today: date) -> int:
    """Re-derive every Treasury security's terms from its stored auctions (no upstream reads). Commits."""
    ids = list(s.scalars(select(Auction.sec_id).distinct().order_by(Auction.sec_id)))
    recorded = 0
    for n, sec_id in enumerate(ids, 1):
        recorded += rebuild_security(s, sec_id, now, today)["terms_recorded"]
        if n % 500 == 0:
            s.commit()
            s.expunge_all()
    s.commit()
    return recorded


def notable_checks(s: Session, limit: int = 25) -> list[dict]:
    """Securities whose checks say more than "load more history": for reading in the job's answer."""
    out = []
    rows = s.execute(select(SecurityTerms.sec_id, SecurityTerms.checks).where(SecurityTerms.superseded_at.is_(None))
                     .order_by(SecurityTerms.sec_id))
    for sec_id, checks in rows:
        notable = [c for c in checks or [] if tr.check_code(c) not in tr.EXPECTED_CHECKS]
        if notable:
            name = s.scalar(select(InstrumentName.name).where(
                InstrumentName.sec_id == sec_id, InstrumentName.kind == "short", InstrumentName.removed_at.is_(None)))
            out.append({"short_name": name, "checks": notable})
            if len(out) >= limit:
                break
    return out


def _thousands(fields: dict, k: str):
    v = fields.get(k)
    if v in (None, "", "null"):
        return None
    try:
        return Decimal(v) * 1000
    except InvalidOperation:
        raise tr.Untypable(f"{k}: {v!r} isn't a number") from None


def apply_mspd_period(s: Session, source: str, period: str, recs: list[Rec], now: datetime, today: date) -> dict:
    """One month-end of the MSPD's stripped-securities table into stripped_amount. Flushes, doesn't commit.

    The strip instruments themselves are synced once per load (sync_strips), from these and the terms.
    """
    have = {a.source_key: a for a in s.scalars(select(StrippedAmount).where(StrippedAmount.period == period))}
    out = {"records": len(recs), "added": 0, "updated": 0, "removed": 0, "untyped": 0, "created": 0,
           "terms_recorded": 0}
    seen = set()
    for r in recs:
        if r.record_type != "stripped_security":
            continue  # the table's subtotal lines
        f = r.fields
        try:
            if not f.get("cusip") or f.get("security_class2_desc") in (None, "", "null"):
                raise tr.Untypable("no principal STRIPS or underlying CUSIP")
            values = {
                "strip_cusip": f["cusip"], "underlying_cusip": f["security_class2_desc"],
                "record_date": date.fromisoformat(f["record_date"][:10]),
                "outstanding": _thousands(f, "outstanding_amt"), "unstripped": _thousands(f, "portion_unstripped_amt"),
                "stripped": _thousands(f, "portion_stripped_amt"), "reconstituted": _thousands(f, "reconstituted_amt"),
            }
        except (tr.Untypable, ValueError, KeyError) as e:
            _untyped(s, source, period, r.source_key, str(e), now)
            out["untyped"] += 1
            continue
        seen.add(r.source_key)
        row = have.get(r.source_key)
        if row is None:
            s.add(StrippedAmount(period=period, source_key=r.source_key, **values, fields=f, record_id=r.record_id,
                                 capture_id=r.capture_id, loaded_at=now))
            out["added"] += 1
        elif row.removed_at is not None or row.fields != f:
            for k, v in values.items():
                setattr(row, k, v)
            row.fields, row.removed_at, row.loaded_at = f, None, now
            out["updated"] += 1
        row = have.get(r.source_key)
        if row is not None:
            row.record_id, row.capture_id = r.record_id, r.capture_id
    for key, row in have.items():
        if key not in seen and row.removed_at is None:
            row.removed_at = now
            out["removed"] += 1
    s.flush()
    return out


APPLY = {"TD-SECURITIES": apply_period, "FD-MSPD-STRIPS": apply_mspd_period}


def sync_strips(s: Session, today: date, now: datetime) -> dict:
    """Principal and interest STRIPS as instruments, from the terms, the auctions' tint CUSIPs and the MSPD.

    A strip is created the first time a source names it and kept in line after
    that (its row in `strip`, CUSIP and ISIN identifiers, short name, status).
    """
    specs: list[strips.StripSpec] = []
    terms = {t.sec_id: t for t in s.scalars(select(SecurityTerms).where(SecurityTerms.superseded_at.is_(None)))}
    for t in terms.values():
        src = (t.provenance or {}).get("corpus_cusip", "")
        key = src.split(" ")[2] if src.startswith("published:") else t.cusip
        spec = strips.from_security(t.cusip, t.security_type, t.maturity_date, t.corpus_cusip, key)
        if spec:
            specs.append(spec)
    tint = Auction.fields["tintCusip1"].as_string()
    for sec_id, key, fields in s.execute(select(Auction.sec_id, Auction.source_key, Auction.fields)
                                         .where(Auction.removed_at.is_(None), tint.is_not(None), tint != "")):
        t = terms.get(sec_id)
        if t is not None:
            specs.extend(strips.from_auction(t.cusip, t.security_type, t.maturity_date, fields, key))
    latest = select(StrippedAmount.strip_cusip, func.max(StrippedAmount.id).label("id")).where(
        StrippedAmount.removed_at.is_(None)).group_by(StrippedAmount.strip_cusip).subquery()
    for key, fields in s.execute(select(StrippedAmount.source_key, StrippedAmount.fields)
                                 .join(latest, StrippedAmount.id == latest.c.id)):
        spec = strips.from_mspd(fields, key)
        if spec:
            specs.append(spec)
    merged = strips.merge(specs)
    by_cusip = dict(s.execute(select(Identifier.value, Identifier.sec_id).where(
        Identifier.scheme == "CUSIP", Identifier.removed_at.is_(None))).all())
    rows = {r.cusip: r for r in s.scalars(select(Strip))}
    out = {"strips_created": 0, "strips_updated": 0}
    for cusip, spec in sorted(merged.items()):
        sec_id = by_cusip.get(cusip)
        if sec_id is None:
            inst = Instrument(type=strips.KINDS[spec.kind], currency="USD", country="US", curve="UST", tenor=None,
                              calendar="SIFMA-US", status="active", description="")
            s.add(inst)
            s.flush()
            sec_id = inst.sec_id
            s.add(Identifier(sec_id=sec_id, scheme="CUSIP", value=cusip))
            out["strips_created"] += 1
        under_id = by_cusip.get(spec.underlying_cusip) if spec.underlying_cusip else None
        if spec.underlying_cusip and under_id is None:
            spec.checks = sorted(set(spec.checks + ["underlying-not-loaded: its security isn't in the security master"]))
        values = {"cusip": cusip, "kind": spec.kind, "tips": spec.tips, "payment_date": spec.payment_date,
                  "underlying_cusip": spec.underlying_cusip, "underlying_sec_id": under_id,
                  "provenance": spec.provenance, "checks": spec.checks}
        row = rows.get(cusip)
        if row is None:
            s.add(Strip(sec_id=sec_id, **values, updated_at=now))
        elif any(getattr(row, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(row, k, v)
            row.updated_at = now
            out["strips_updated"] += 1
        inst = s.get(Instrument, sec_id)
        under_name = s.scalar(select(InstrumentName.name).where(
            InstrumentName.sec_id == under_id, InstrumentName.kind == "short",
            InstrumentName.removed_at.is_(None))) if under_id else None
        status = "matured" if spec.payment_date and spec.payment_date < today else "active"
        attrs = {"type": strips.KINDS[spec.kind], "description": strips.describe(spec, under_name or spec.underlying_cusip),
                 "status": status}
        if any(getattr(inst, k) != v for k, v in attrs.items()):
            for k, v in attrs.items():
                setattr(inst, k, v)
            inst.updated_at = now
        _ensure_identifier(s, sec_id, "ISIN", tr.isin(cusip))
        _set_short_name(s, sec_id, strips.short_name(spec), cusip, now)
        by_cusip[cusip] = sec_id
    s.flush()
    out["strips"] = len(merged)
    return out


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
            result = APPLY[source](s, source, p.period, up.get_period(source, p.period), now, today)
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
            result = APPLY[source](s, source, period, [], now, today)
            s.execute(delete(SourcePeriod).where(SourcePeriod.source == source, SourcePeriod.period == period))
            s.commit()
            progress["periods_read"] += 1
            progress["removed"] += result["removed"]
    if _typing_version(s) != tr.TYPING_VERSION:
        progress["retyped_terms_recorded"] = retype_all(s, now, today)
    progress["status"] = refresh_status(s, today, now)
    progress |= sync_otr(s, today, now)
    progress |= sync_strips(s, today, now)
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
    progress["typing_version"] = tr.TYPING_VERSION
    progress["notable_checks"] = notable_checks(s)
    s.add(LoadRun(started_at=now, finished_at=datetime.now(UTC), outcome="ok", detail=json.dumps(progress)))
    s.commit()
    return progress


def rebuild(s: Session, up: Upstream, now: datetime | None = None, today: date | None = None) -> dict:
    """Re-read every period: watermarks cleared, then a load. sec_ids are kept (identity is the CUSIP)."""
    s.execute(delete(SourcePeriod))
    s.commit()
    return run(s, up, now, today)
