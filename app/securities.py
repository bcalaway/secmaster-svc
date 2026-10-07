"""Lookups for other services (proto/securities.proto) and the job API.

Plain functions returning plain dicts, tested without gRPC. Names are matched
case-insensitively (they're stored upper-case). Only current rows count:
a removed name, identifier or note doesn't resolve.
"""

import re
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Auction, Identifier, Instrument, InstrumentName, InstrumentNote, SecurityTerms


class UnknownInstrument(LookupError):
    pass


_UNIT_DAYS = {"D": 1, "W": 7, "M": 30.4375, "Y": 365.25}


def tenor_days(tenor: str | None) -> float:
    """Approximate length of an ISO 8601 duration, for ordering a curve."""
    if not tenor:
        return float("inf")
    return sum(float(n) * _UNIT_DAYS[u] for n, _, u in re.findall(r"(\d+(\.\d+)?)([DWMY])", tenor))


def _iso(d) -> str:
    return d.isoformat() if d else ""


def _names(s: Session, sec_ids) -> dict[int, dict]:
    out = {i: {"short_name": "", "aliases": []} for i in sec_ids}
    rows = s.scalars(select(InstrumentName).where(
        InstrumentName.sec_id.in_(list(sec_ids)), InstrumentName.removed_at.is_(None)).order_by(InstrumentName.name))
    for r in rows:
        if r.kind == "short":
            out[r.sec_id]["short_name"] = r.name
        else:
            out[r.sec_id]["aliases"].append(r.name)
    return out


def _describe(s: Session, insts: list[Instrument], full: bool) -> list[dict]:
    names = _names(s, [i.sec_id for i in insts])
    ids: dict[int, list] = {i.sec_id: [] for i in insts}
    notes: dict[int, list] = {i.sec_id: [] for i in insts}
    if full and insts:
        for r in s.scalars(select(Identifier).where(
                Identifier.sec_id.in_(list(ids)), Identifier.removed_at.is_(None))
                .order_by(Identifier.scheme, Identifier.value)):
            ids[r.sec_id].append({"scheme": r.scheme, "value": r.value,
                                  "valid_from": _iso(r.valid_from), "valid_to": _iso(r.valid_to)})
        for r in s.scalars(select(InstrumentNote).where(
                InstrumentNote.sec_id.in_(list(notes)), InstrumentNote.removed_at.is_(None))
                .order_by(InstrumentNote.on_date, InstrumentNote.key)):
            notes[r.sec_id].append({"key": r.key, "date": _iso(r.on_date), "text": r.text})
    out = []
    for i in insts:
        d = {
            "sec_id": i.sec_id, **names[i.sec_id], "type": i.type, "currency": i.currency,
            "country": i.country, "curve": i.curve or "", "tenor": i.tenor or "", "calendar": i.calendar,
            "status": i.status, "description": i.description,
        }
        if full:
            d |= {"identifiers": ids[i.sec_id], "notes": notes[i.sec_id]}
        out.append(d)
    return out


def _order(insts: list[Instrument]) -> list[Instrument]:
    return sorted(insts, key=lambda i: (i.type, i.curve or "", tenor_days(i.tenor), i.sec_id))


def get(s: Session, sec_id: int | None = None, name: str = "") -> dict:
    """One instrument by sec_id, or by short name or alias, with its identifiers and notes."""
    if sec_id:
        inst = s.get(Instrument, sec_id)
    else:
        row = s.scalar(select(InstrumentName).where(
            InstrumentName.name == name.strip().upper(), InstrumentName.removed_at.is_(None)))
        inst = s.get(Instrument, row.sec_id) if row else None
    if inst is None:
        raise UnknownInstrument(f"no instrument {sec_id or name!r}")
    out = _describe(s, [inst], full=True)[0]
    terms = s.scalar(select(SecurityTerms).where(SecurityTerms.sec_id == inst.sec_id,
                                                 SecurityTerms.superseded_at.is_(None)))
    if terms is not None:
        out["terms"] = _plain(terms, skip=("id", "sec_id", "provenance", "checks", "superseded_at"))
        out["provenance"], out["checks"] = terms.provenance, terms.checks
        out["auctions"] = [
            _plain(a, skip=("id", "sec_id", "fields", "source", "period", "loaded_at", "updated_at", "removed_at"))
            for a in s.scalars(select(Auction).where(Auction.sec_id == inst.sec_id, Auction.removed_at.is_(None))
                               .order_by(Auction.issue_date, Auction.source_key))
        ]
    return out


def _plain(row, skip=()) -> dict:
    """A row's columns as JSON-ready values: dates ISO, decimals as strings exactly as stored."""
    out = {}
    for c in row.__table__.columns:
        if c.name in skip:
            continue
        v = getattr(row, c.name)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif v is not None and not isinstance(v, (bool, int, str, dict, list)):
            v = format(v, "f")
        out[c.name] = v
    return out


def list_instruments(s: Session, type: str = "", curve: str = "", include_inactive: bool = False,
                     full: bool = False) -> list[dict]:
    """Instruments, by type, curve and tenor."""
    q = select(Instrument)
    if type:
        q = q.where(Instrument.type == type)
    if curve:
        q = q.where(Instrument.curve == curve.upper())
    if not include_inactive:
        q = q.where(Instrument.status == "active")
    return _describe(s, _order(list(s.scalars(q))), full=full)


def resolve(s: Session, scheme: str, values: list[str], as_of: date | None = None) -> dict:
    """Map a source's keys to instruments: the batch lookup quote-svc uses.

    With as_of, only identifiers valid on that date count; without it, any
    current one. Unknown values are listed, not an error: quote-svc reports
    them as unmapped.
    """
    q = select(Identifier).where(Identifier.scheme == scheme, Identifier.value.in_(values),
                                 Identifier.removed_at.is_(None))
    if as_of:
        q = q.where(or_(Identifier.valid_from.is_(None), Identifier.valid_from <= as_of),
                    or_(Identifier.valid_to.is_(None), Identifier.valid_to >= as_of))
    rows = list(s.scalars(q))
    names = _names(s, {r.sec_id for r in rows})
    found = {}
    for r in rows:
        found.setdefault(r.value, []).append(
            {"value": r.value, "sec_id": r.sec_id, "short_name": names[r.sec_id]["short_name"],
             "valid_from": _iso(r.valid_from), "valid_to": _iso(r.valid_to)})
    matches = [m for v in values if v in found for m in found[v]]
    return {"scheme": scheme, "matches": matches, "unknown": [v for v in values if v not in found]}


def search(s: Session, query: str, limit: int = 20) -> list[dict]:
    """Instruments whose name, alias, identifier or description contains the query."""
    q = query.strip()
    if not q:
        return []
    like = f"%{q}%"
    ids = set(s.scalars(select(InstrumentName.sec_id).where(
        InstrumentName.name.ilike(like), InstrumentName.removed_at.is_(None))))
    ids |= set(s.scalars(select(Identifier.sec_id).where(
        Identifier.value.ilike(like), Identifier.removed_at.is_(None))))
    ids |= set(s.scalars(select(Instrument.sec_id).where(Instrument.description.ilike(like))))
    insts = _order(list(s.scalars(select(Instrument).where(Instrument.sec_id.in_(ids))))) if ids else []
    return _describe(s, insts[:limit], full=False)
