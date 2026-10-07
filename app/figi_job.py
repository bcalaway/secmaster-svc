"""The FIGI job: ask OpenFIGI about Treasury CUSIPs not yet looked up, and keep the answers as identifiers.

Schemes: FIGI, COMPOSITE-FIGI (when OpenFIGI gives one) and TICKER (Bloomberg
style: "T 4 1/4 08/15/35"). A ticker two instruments share (principal STRIPS
of a note and a bond due the same day can) is kept on the first and listed in
the job's answer, not treated as an error. Each CUSIP OpenFIGI answered with an
error is listed in the answer with OpenFIGI's own words, so the DAG's log names it.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import figi, strips
from app.load import TREASURY_TYPES
from app.models import FigiLookup, Identifier, Instrument

TYPES = [*TREASURY_TYPES, *strips.KINDS.values()]
MAX_WITH_KEY = 10_000


def _soft_identifier(s: Session, sec_id: int, scheme: str, value: str | None) -> bool:
    """Add (scheme, value) for sec_id unless it's there; False if another instrument already has it."""
    if not value:
        return True
    owner = s.scalar(select(Identifier.sec_id).where(Identifier.scheme == scheme, Identifier.value == value,
                                                     Identifier.removed_at.is_(None)))
    if owner is None:
        s.add(Identifier(sec_id=sec_id, scheme=scheme, value=value))
        s.flush()
        return True
    return owner == sec_id


def run(s: Session, api_key: str | None, now: datetime | None = None, mapper=None) -> dict:
    now = now or datetime.now(UTC)
    mapper = mapper or figi.map_cusips
    cusips = dict(s.execute(select(Identifier.value, Identifier.sec_id)
                            .join(Instrument, Instrument.sec_id == Identifier.sec_id)
                            .where(Identifier.scheme == "CUSIP", Identifier.removed_at.is_(None),
                                   Instrument.type.in_(TYPES))).all())
    seen = {r.cusip: r for r in s.scalars(select(FigiLookup))}
    todo = sorted(c for c in cusips if figi.due(seen[c].looked_up_at if c in seen else None,
                                                seen[c].outcome if c in seen else None, now))
    limit = MAX_WITH_KEY if api_key else figi.MAX_WITHOUT_KEY
    batch = todo[:limit]
    out = {"asked": len(batch), "left": len(todo) - len(batch), "with_key": bool(api_key),
           "found": 0, "not_found": 0, "error": 0, "errors": [], "shared_tickers": []}
    answers = mapper(batch, api_key) if batch else []
    for a in answers:
        sec_id = cusips[a.cusip]
        row = seen.get(a.cusip) or FigiLookup(cusip=a.cusip, sec_id=sec_id)
        row.sec_id, row.outcome, row.figi, row.composite_figi, row.ticker = (
            sec_id, a.outcome, a.figi, a.composite_figi, a.ticker)
        row.detail, row.looked_up_at = json.loads(json.dumps(a.detail)) if a.detail else None, now
        s.merge(row)
        out[a.outcome] += 1
        if a.outcome == "error":
            out["errors"].append({"cusip": a.cusip, "detail": row.detail})
        if a.outcome == "found":
            _soft_identifier(s, sec_id, "FIGI", a.figi)
            if a.composite_figi and a.composite_figi != a.figi:
                _soft_identifier(s, sec_id, "COMPOSITE-FIGI", a.composite_figi)
            if not _soft_identifier(s, sec_id, "TICKER", a.ticker):
                out["shared_tickers"].append({"cusip": a.cusip, "ticker": a.ticker})
    s.commit()
    out["shared_tickers"] = out["shared_tickers"][:20]
    out["errors"] = out["errors"][:20]
    out["mapped"] = s.scalar(select(func.count()).select_from(FigiLookup).where(FigiLookup.outcome == "found"))
    return out
