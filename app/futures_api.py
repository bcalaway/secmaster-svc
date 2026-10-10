"""Futures for the screens and voice (mkt-data's docs/phase-4.md, step 7): products, a product's contracts and
today's generics, a Treasury contract's deliverable basket (with each security's amount outstanding from MSPD's
latest month), and the contracts a security is deliverable into. Read-only, from what the futures job stored.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    FuturesContract,
    FuturesDeliverable,
    FuturesProduct,
    Identifier,
    Instrument,
    InstrumentName,
    SecurityTerms,
    StrippedAmount,
)
from app.securities import UnknownInstrument, _names, today_ny

DATES = ("first_trade_date", "last_trade_date", "first_intention_date", "first_notice_date", "first_delivery_date",
         "last_delivery_date", "reference_start", "reference_end", "final_settlement_date", "settlement_date")
CONTRACT_STATUSES = ("listed", "delivery")  # what "current" means; expired and withdrawn only when asked


def _iso(d) -> str:
    return d.isoformat() if d else ""


def _valid_on(on: date):
    return and_(or_(Identifier.valid_from.is_(None), Identifier.valid_from <= on),
                or_(Identifier.valid_to.is_(None), Identifier.valid_to >= on))


def _generics(s: Session, product_sec_id: int, on: date) -> list[dict]:
    """Each generic (TY1, TY2, ...) on a date with the contract it is."""
    rows = s.execute(select(Identifier.value, Identifier.sec_id).join(
        FuturesContract, and_(FuturesContract.sec_id == Identifier.sec_id, FuturesContract.superseded_at.is_(None)))
        .where(FuturesContract.product_sec_id == product_sec_id, Identifier.scheme == "GENERIC",
               Identifier.removed_at.is_(None), _valid_on(on))).all()
    names = _names(s, {sec_id for _, sec_id in rows})
    out = [{"generic": g, "contract": names[sec_id]["short_name"]} for g, sec_id in rows]
    return sorted(out, key=lambda x: (len(x["generic"]), x["generic"]))


def _summary(s: Session, p: FuturesProduct, inst: Instrument, on: date) -> dict:
    info = p.info or {}
    gens = _generics(s, p.sec_id, on)
    listed = s.scalar(select(FuturesContract.id).where(  # any listed at all
        FuturesContract.product_sec_id == p.sec_id, FuturesContract.superseded_at.is_(None),
        FuturesContract.status == "listed").limit(1))
    return {"root": p.root, "cme_code": p.cme_code, "name": info.get("name", inst.description), "kind": p.kind,
            "currency": inst.currency, "cftc_code": (info.get("cftc") or {}).get("code", ""),
            "front": next((g["contract"] for g in gens if g["generic"] == f"{p.root}1"), ""),
            "status": "listed" if listed else "none listed"}


def products(s: Session, on: date | None = None) -> list[dict]:
    """Every futures product, by kind then root."""
    on = on or today_ny()
    rows = s.execute(select(FuturesProduct, Instrument).join(Instrument, Instrument.sec_id == FuturesProduct.sec_id)
                     .order_by(FuturesProduct.kind, FuturesProduct.root)).all()
    return [_summary(s, p, inst, on) for p, inst in rows]


def _product_row(s: Session, root: str) -> tuple[FuturesProduct, Instrument]:
    got = s.execute(select(FuturesProduct, Instrument).join(Instrument, Instrument.sec_id == FuturesProduct.sec_id)
                    .where(FuturesProduct.root == root.strip().upper())).first()
    if got is None:
        raise UnknownInstrument(f"no futures product {root!r}")
    return got[0], got[1]


def product(s: Session, root: str, include_expired: bool = False, on: date | None = None) -> dict:
    """One product: what the seed says (rules, sources, basket rule), its contracts (listed and delivering, or every
    one), each with its CME code and dates, and today's generics."""
    on = on or today_ny()
    p, inst = _product_row(s, root)
    q = select(FuturesContract).where(FuturesContract.product_sec_id == p.sec_id,
                                      FuturesContract.superseded_at.is_(None))
    if not include_expired:
        q = q.where(FuturesContract.status.in_(CONTRACT_STATUSES))
    contracts = list(s.scalars(q.order_by(FuturesContract.contract_month)))
    names = _names(s, [c.sec_id for c in contracts])
    codes = dict(s.execute(select(Identifier.sec_id, Identifier.value).where(
        Identifier.sec_id.in_([c.sec_id for c in contracts]), Identifier.scheme == "CME",
        Identifier.removed_at.is_(None))).all()) if contracts else {}
    counts: dict[int, int] = {}
    if contracts and p.kind == "treasury":
        for (cid,) in s.execute(select(FuturesDeliverable.contract_sec_id).where(
                FuturesDeliverable.contract_sec_id.in_([c.sec_id for c in contracts]),
                FuturesDeliverable.superseded_at.is_(None))):
            counts[cid] = counts.get(cid, 0) + 1
    info = p.info or {}
    return {
        **_summary(s, p, inst, on),
        "rules": dict(info.get("rules") or {}), "rule_sources": dict(info.get("rule_sources") or {}),
        "basket_rule": (info.get("basket") or {}).get("rule", ""),
        "basket_source": (info.get("basket") or {}).get("source", ""),
        "generics": _generics(s, p.sec_id, on),
        "contracts": [{"name": names[c.sec_id]["short_name"], "cme_code": codes.get(c.sec_id, ""),
                       "month": c.contract_month.isoformat()[:7], "status": c.status,
                       "basket_size": counts.get(c.sec_id, 0) if p.kind == "treasury" else -1,
                       **{f: _iso(getattr(c, f)) for f in DATES}} for c in contracts],
    }


def basket(s: Session, name: str) -> dict:
    """A Treasury futures contract's deliverable basket, by maturity, with each security's conversion factor."""
    key = name.strip().upper()
    sec_id = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == key,
                                                          InstrumentName.removed_at.is_(None)))
    contract = s.scalar(select(FuturesContract).where(FuturesContract.sec_id == sec_id,
                                                      FuturesContract.superseded_at.is_(None))) if sec_id else None
    if contract is None:
        raise UnknownInstrument(f"no futures contract {name!r}")
    p = s.get(FuturesProduct, contract.product_sec_id)
    rows = s.execute(select(FuturesDeliverable, SecurityTerms).join(
        SecurityTerms, and_(SecurityTerms.sec_id == FuturesDeliverable.security_sec_id,
                            SecurityTerms.superseded_at.is_(None)))
        .where(FuturesDeliverable.contract_sec_id == contract.sec_id, FuturesDeliverable.superseded_at.is_(None))
        .order_by(SecurityTerms.maturity_date, SecurityTerms.cusip)).all()
    names = _names(s, [d.security_sec_id for d, _ in rows] + [contract.sec_id])
    mspd = latest_outstanding(s, [t.cusip for _, t in rows])
    amounts = [mspd.get(t.cusip) for _, t in rows]
    deliverables = [{
        "security": names[d.security_sec_id]["short_name"], "cusip": t.cusip,
        "coupon_rate": format(t.coupon_rate.normalize(), "f") if t.coupon_rate is not None else "",
        "maturity_date": _iso(t.maturity_date), "issue_date": _iso(t.issue_date),
        "conversion_factor": format(d.conversion_factor, "f"), "remaining_months": d.remaining_months,
        "valid_from": _iso(d.valid_from),
        "outstanding": _dec(a.outstanding) if a and a.outstanding is not None else "",
        "unstripped": _dec(a.unstripped) if a and a.unstripped is not None else "",
        "outstanding_as_of": _iso(a.record_date) if a else ""} for (d, t), a in zip(rows, amounts, strict=True)]
    return {
        "contract": names[contract.sec_id]["short_name"], "product": p.root if p else "",
        "month": contract.contract_month.isoformat()[:7], "status": contract.status,
        "rule": ((p.info or {}).get("basket") or {}).get("rule", "") if p else "",
        "deliverables": deliverables,
        "outstanding_total": _dec(sum((a.outstanding for a in amounts if a and a.outstanding is not None), Decimal(0)))
        if any(a and a.outstanding is not None for a in amounts) else "",
        "unstripped_total": _dec(sum((a.unstripped for a in amounts if a and a.unstripped is not None), Decimal(0)))
        if any(a and a.unstripped is not None for a in amounts) else "",
    }


def _dec(v: Decimal) -> str:
    """A Decimal as a plain string, no exponent (72000000000, not 7.2E+10)."""
    return format(v.normalize(), "f")


def latest_outstanding(s: Session, cusips: list[str]) -> dict[str, StrippedAmount]:
    """Each security's MSPD row for its latest month (amount outstanding and the part not held as STRIPS), by CUSIP.

    MSPD's stripped-securities table covers every STRIPS-eligible note and bond, so a basket security without a row
    is one MSPD hasn't listed yet (a new issue before its first month-end).
    """
    if not cusips:
        return {}
    live = and_(StrippedAmount.underlying_cusip.in_(cusips), StrippedAmount.removed_at.is_(None))
    latest = (select(StrippedAmount.underlying_cusip, func.max(StrippedAmount.record_date).label("record_date"))
              .where(live).group_by(StrippedAmount.underlying_cusip).subquery())
    rows = s.scalars(select(StrippedAmount).join(latest, and_(
        StrippedAmount.underlying_cusip == latest.c.underlying_cusip,
        StrippedAmount.record_date == latest.c.record_date)).where(live).order_by(StrippedAmount.id))
    return {r.underlying_cusip: r for r in rows}


def deliverable_into(s: Session, security_sec_id: int, statuses: tuple[str, ...] = CONTRACT_STATUSES) -> list[dict]:
    """The listed or delivering Treasury futures contracts a security is deliverable into, by product and month."""
    rows = s.execute(select(FuturesDeliverable, FuturesContract).join(
        FuturesContract, and_(FuturesContract.sec_id == FuturesDeliverable.contract_sec_id,
                              FuturesContract.superseded_at.is_(None)))
        .where(FuturesDeliverable.security_sec_id == security_sec_id, FuturesDeliverable.superseded_at.is_(None),
               FuturesContract.status.in_(statuses))).all()
    if not rows:
        return []
    roots = dict(s.execute(select(FuturesProduct.sec_id, FuturesProduct.root)
                           .where(FuturesProduct.sec_id.in_({c.product_sec_id for _, c in rows}))).all())
    names = _names(s, [c.sec_id for _, c in rows])
    out = [{"contract": names[c.sec_id]["short_name"], "product": roots.get(c.product_sec_id, ""),
            "month": c.contract_month.isoformat()[:7], "status": c.status,
            "conversion_factor": format(d.conversion_factor, "f"), "remaining_months": d.remaining_months,
            "valid_from": _iso(d.valid_from), "last_delivery_date": _iso(c.last_delivery_date)} for d, c in rows]
    return sorted(out, key=lambda x: (x["product"], x["month"]))
