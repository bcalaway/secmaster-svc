"""Lookups for other services (proto/securities.proto) and the job API.

Plain functions returning plain dicts, tested without gRPC. Names are matched
case-insensitively (they're stored upper-case). Only current rows count:
a removed name, identifier or note doesn't resolve.
"""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Auction,
    FuturesContract,
    FuturesProduct,
    FuturesSpec,
    Identifier,
    Instrument,
    InstrumentName,
    InstrumentNote,
    ReferenceCpi,
    SecurityTerms,
    Strip,
    StrippedAmount,
)

NEW_YORK = ZoneInfo("America/New_York")


def today_ny() -> date:
    """Today in New York, the Treasury market's day."""
    return datetime.now(NEW_YORK).date()


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


def _names(s: Session, sec_ids, every: bool = False) -> dict[int, dict]:
    """Each instrument's short name and aliases. `every`: the caller wants (nearly) all of them, so read the
    whole table rather than send thousands of ids in an IN list."""
    out = {i: {"short_name": "", "aliases": []} for i in sec_ids}
    q = select(InstrumentName.sec_id, InstrumentName.name, InstrumentName.kind).where(
        InstrumentName.removed_at.is_(None))
    if not every:
        q = q.where(InstrumentName.sec_id.in_(list(sec_ids)))
    for sec_id, name, kind in s.execute(q.order_by(InstrumentName.name)):
        if sec_id not in out:
            continue
        if kind == "short":
            out[sec_id]["short_name"] = name
        else:
            out[sec_id]["aliases"].append(name)
    return out


# The columns a list shows. Lists read these as plain rows, not ORM objects: an Instrument object (and a
# SecurityTerms one, with its provenance and checks) costs several times its columns, and at ~10,000
# instruments that was enough to take the 256 MB container past its limit (2026-10-09).
SUMMARY = (Instrument.sec_id, Instrument.type, Instrument.currency, Instrument.country, Instrument.curve,
           Instrument.tenor, Instrument.calendar, Instrument.status, Instrument.description)


def _describe(s: Session, insts: list, full: bool, every: bool = False) -> list[dict]:
    """Instruments (ORM objects or SUMMARY rows) as dicts; `full` adds identifiers and notes."""
    names = _names(s, [i.sec_id for i in insts], every=every)
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


def get(s: Session, sec_id: int | None = None, name: str = "", as_of: date | None = None) -> dict:
    """One instrument by sec_id, or by short name or alias, with its identifiers and notes.

    An on-the-run name (UST-10Y-OTR) resolves to the security on the run on
    `as_of` (default today), and a futures generic (TY1) to that day's contract.
    """
    if sec_id:
        inst = s.get(Instrument, sec_id)
    else:
        key = name.strip().upper()
        row = s.scalar(select(InstrumentName).where(InstrumentName.name == key, InstrumentName.removed_at.is_(None)))
        target = row.sec_id if row else _on_the_run(s, key, as_of or today_ny())
        inst = s.get(Instrument, target) if target else None
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
        if terms.security_type == "tips" and terms.tips_base_cpi:
            out["index_ratio"] = index_ratio(s, terms.tips_base_cpi, as_of or today_ny())
        out["stripped_amounts"] = [
            _plain(a, skip=("id", "fields", "period", "source_key", "record_id", "capture_id", "loaded_at",
                            "removed_at", "underlying_cusip"))
            for a in s.scalars(select(StrippedAmount).where(
                StrippedAmount.underlying_cusip == terms.cusip, StrippedAmount.removed_at.is_(None))
                .order_by(StrippedAmount.record_date.desc()).limit(12))
        ]
    strip = s.get(Strip, inst.sec_id)
    if strip is not None:
        out["strip"] = _plain(strip, skip=("sec_id", "provenance", "checks", "updated_at"))
        out["provenance"], out["checks"] = strip.provenance, strip.checks
    product = s.get(FuturesProduct, inst.sec_id)
    if product is not None:
        out["futures_product"] = product.info
        out["specs"] = [_plain(x, skip=("id", "sec_id", "superseded_at")) for x in s.scalars(
            select(FuturesSpec).where(FuturesSpec.sec_id == inst.sec_id, FuturesSpec.superseded_at.is_(None))
            .order_by(FuturesSpec.valid_from))]
    contract = s.scalar(select(FuturesContract).where(FuturesContract.sec_id == inst.sec_id,
                                                      FuturesContract.superseded_at.is_(None)))
    if contract is not None:
        out["contract"] = _plain(contract, skip=("id", "sec_id", "rules", "superseded_at"))
        out["contract"]["product"] = _names(s, [contract.product_sec_id])[contract.product_sec_id]["short_name"]
        out["provenance"] = contract.rules
    return out


def reference_cpi(s: Session, on: date) -> dict | None:
    row = s.get(ReferenceCpi, on)
    return None if row is None else {"date": on.isoformat(), "ref_cpi": format(row.value, "f"), "method": row.method}


def index_ratio(s: Session, base_cpi, on: date) -> dict | None:
    """A TIPS's index ratio on a date: reference CPI over its base, both as Treasury rounds them."""
    from app.tips import index_ratio as ratio

    ref = reference_cpi(s, on)
    if ref is None:
        return None
    return ref | {"base_cpi": format(base_cpi, "f"), "index_ratio": format(ratio(s.get(ReferenceCpi, on).value,
                                                                                    base_cpi), "f")}


def _on_the_run(s: Session, name: str, on: date) -> int | None:
    """An on-the-run alias (UST-10Y-OTR) or a futures generic (TY1) on a date."""
    return s.scalar(select(Identifier.sec_id).where(
        Identifier.scheme.in_(("OTR", "GENERIC")), Identifier.value == name, Identifier.removed_at.is_(None),
        or_(Identifier.valid_from.is_(None), Identifier.valid_from <= on),
        or_(Identifier.valid_to.is_(None), Identifier.valid_to >= on)))


def on_the_run(s: Session, on: date) -> list[dict]:
    """Every on-the-run alias on a date, with its security's short name."""
    rows = list(s.scalars(select(Identifier).where(
        Identifier.scheme == "OTR", Identifier.removed_at.is_(None),
        or_(Identifier.valid_from.is_(None), Identifier.valid_from <= on),
        or_(Identifier.valid_to.is_(None), Identifier.valid_to >= on)).order_by(Identifier.value)))
    names = _names(s, {r.sec_id for r in rows})
    return [{"alias": r.value, "sec_id": r.sec_id, "short_name": names[r.sec_id]["short_name"],
             "since": _iso(r.valid_from), "until": _iso(r.valid_to)} for r in rows]


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
    q = select(*SUMMARY)
    if type:
        q = q.where(Instrument.type == type)
    if curve:
        q = q.where(Instrument.curve == curve.upper())
    if not include_inactive:
        q = q.where(Instrument.status == "active")
    rows = _order(list(s.execute(q)))
    return _describe(s, rows, full=full, every=not (type or curve) and len(rows) > 500)


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
    insts = _order(list(s.execute(select(*SUMMARY).where(Instrument.sec_id.in_(ids))))) if ids else []
    return _describe(s, insts[:limit], full=False)


# --- Treasury securities (mkt-data's docs/phase-3.md, step 9): lists for screens, and one security in full ---

SECURITY_TYPES = ("bill", "note", "bond", "tips", "frn")
LIST_LIMIT = 1000
LIST_MAX = 6000


def _dec(v) -> str:
    """A decimal as plain text without trailing zeros (0.0375, not 0.0375000000); "" for none."""
    return "" if v is None else format(v.normalize(), "f")


def _otr(s: Session, sec_ids, on: date) -> dict[int, list[dict]]:
    """Each security's on-the-run aliases valid on a date."""
    out: dict[int, list[dict]] = {i: [] for i in sec_ids}
    if not out:
        return out
    for r in s.scalars(select(Identifier).where(
            Identifier.scheme == "OTR", Identifier.removed_at.is_(None), Identifier.sec_id.in_(list(out)),
            or_(Identifier.valid_from.is_(None), Identifier.valid_from <= on),
            or_(Identifier.valid_to.is_(None), Identifier.valid_to >= on)).order_by(Identifier.value)):
        out[r.sec_id].append({"alias": r.value, "since": _iso(r.valid_from), "until": _iso(r.valid_to)})
    return out


def list_securities(s: Session, security_type: str = "", include_inactive: bool = False,
                    maturing_from: date | None = None, maturing_to: date | None = None,
                    as_of: date | None = None, limit: int = 0) -> dict:
    """Treasury securities by maturity: outstanding ones (active) unless include_inactive, of one type or all.

    Each with the terms a list shows (CUSIP, type, coupon, dates) and its
    on-the-run aliases on `as_of` (default today). `total` counts every match;
    at most `limit` (default LIST_LIMIT, at most LIST_MAX) come back.
    """
    on = as_of or today_ny()
    limit = min(limit or LIST_LIMIT, LIST_MAX)
    t_ = SecurityTerms
    q = (select(t_.sec_id, t_.cusip, t_.security_type, t_.cmb, t_.term, t_.original_term, t_.coupon_rate,
                t_.frn_spread, t_.issue_date, t_.dated_date, t_.maturity_date, Instrument.status, Instrument.description)
         .join(Instrument, Instrument.sec_id == SecurityTerms.sec_id)
         .where(SecurityTerms.superseded_at.is_(None)))
    if security_type:
        q = q.where(SecurityTerms.security_type == security_type.lower())
    if not include_inactive:
        q = q.where(Instrument.status == "active")
    if maturing_from:
        q = q.where(SecurityTerms.maturity_date >= maturing_from)
    if maturing_to:
        q = q.where(SecurityTerms.maturity_date <= maturing_to)
    rows = s.execute(q.order_by(SecurityTerms.maturity_date, SecurityTerms.security_type, SecurityTerms.cusip)).all()
    shown = rows[:limit]
    ids = [t.sec_id for t in shown]
    names = _names(s, ids)
    otr = _otr(s, ids, on)
    out = []
    for t in shown:
        status, description = t.status, t.description
        out.append({
            "sec_id": t.sec_id, "short_name": names[t.sec_id]["short_name"], "cusip": t.cusip,
            "security_type": t.security_type, "cmb": bool(t.cmb), "term": t.term or "",
            "original_term": t.original_term or "",
            "coupon_rate": _dec(t.coupon_rate), "frn_spread": _dec(t.frn_spread),
            "issue_date": _iso(t.issue_date), "dated_date": _iso(t.dated_date), "maturity_date": _iso(t.maturity_date),
            "status": status, "description": description or "",
            "on_the_run": [o["alias"] for o in otr[t.sec_id]],
        })
    return {"as_of": on.isoformat(), "total": len(rows), "securities": out}


def security(s: Session, sec_id: int | None = None, name: str = "", as_of: date | None = None) -> dict:
    """One Treasury security in full (get's answer) plus its on-the-run aliases on `as_of` (default today).

    UnknownInstrument if there's none, or if the instrument isn't a Treasury security or STRIPS.
    """
    out = get(s, sec_id=sec_id, name=name, as_of=as_of)
    if "terms" not in out and "strip" not in out:
        raise UnknownInstrument(f"{out['short_name']} isn't a Treasury security")
    out["on_the_run"] = _otr(s, [out["sec_id"]], as_of or today_ny())[out["sec_id"]]
    return out


AUCTION_FIELDS = ("cusip", "security_type", "term", "security_term", "reopening", "announcement_date",
                  "auction_date", "issue_date", "offering_amount", "total_accepted", "bid_to_cover", "high_yield",
                  "high_discount_rate", "high_discount_margin", "price_per_100")


def list_auctions(s: Session, start: date, end: date, limit: int = 500) -> dict:
    """Auctions held (or announced) from start to end, by auction date: the auction calendar.

    Announced auctions are listed before they're held (TreasuryDirect announces
    them a week or so ahead), without results; held ones with their results.
    """
    rows = list(s.scalars(select(Auction).where(
        Auction.removed_at.is_(None), Auction.auction_date >= start, Auction.auction_date <= end)
        .order_by(Auction.auction_date, Auction.security_type, Auction.cusip).limit(limit)))
    names = _names(s, {a.sec_id for a in rows})
    out = []
    for a in rows:
        row = {"sec_id": a.sec_id, "short_name": names[a.sec_id]["short_name"]}
        for f in AUCTION_FIELDS:
            v = getattr(a, f)
            if isinstance(v, bool):
                row[f] = "true" if v else "false"
            elif hasattr(v, "isoformat"):
                row[f] = v.isoformat()
            elif v is None:
                row[f] = ""
            else:
                row[f] = _dec(v) if not isinstance(v, str) else v
        out.append(row)
    return {"start": start.isoformat(), "end": end.isoformat(), "auctions": out}
