"""FIGIs, Bloomberg tickers and the Bloomberg roots for listed futures contracts, from OpenFIGI
(mkt-data's docs/phase-4.md, step 2b).

Each listed contract (status listed or delivery) is asked two questions in one mapping request:

- **By ticker:** our Bloomberg-style ticker while it trades (`TYZ6`, idType TICKER, securityType2
  Future). Found means Bloomberg has a future with that ticker.
- **By CME's code:** CME's symbol (`ZNZ6`, idType ID_EXCH_SYMBOL, securityType2 Future, which
  OpenFIGI requires with that idType). Its answer carries Bloomberg's ticker for the contract, so
  it confirms the root independently, or names the one Bloomberg uses.

The answers decide the contract's outcome:

- `confirmed`: CME's code maps to our ticker (via `exchange`, or `both` when the ticker question
  found it too), or only the ticker question found it (via `ticker`; weaker, since nothing ties
  that future to CME's code, so the job's answer shows its name for a look).
- `mismatch`: CME's code maps to a future with another root (`bloomberg_root`); nothing is stored,
  and the seed's root is the thing to fix (in a reviewed PR).
- `not_found` or `error` (OpenFIGI's own words kept in `detail`); asked again next run.

For a confirmed contract the job keeps its FIGI (scheme FIGI), composite FIGI when it differs
(COMPOSITE-FIGI) and Bloomberg's live ticker with its market sector (`TYZ6 Comdty`, scheme TICKER),
valid while the contract is listed, the same as its CME code. A confirmed contract isn't asked
again unless its product's root changes; its ticker's validity follows the CME code's each run.

The job's answer lists, per product, how many of its listed contracts each outcome covers and the
roots Bloomberg used, which is what the seed's `root_confirmed` is set from. Runs after the futures
job (dags/futures.py, POST /jobs/futures-figi) with the phase 3 key and client (app/figi.py).
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import figi
from app.figi_job import _soft_identifier
from app.futures import MONTH_CODES
from app.models import FuturesContract, FuturesFigiLookup, FuturesProduct, Identifier, InstrumentName

LIVE = ("listed", "delivery")
# Without a key OpenFIGI takes 25 requests a minute of 10 jobs; two jobs a contract.
MAX_WITHOUT_KEY = 100
TICKER = re.compile(r"^(?P<root>[A-Z0-9]+?)(?P<code>[FGHJKMNQUVXZ])(?P<year>\d{1,2})$")


@dataclass(frozen=True)
class Verdict:
    outcome: str  # confirmed | mismatch | not_found | error
    via: str | None = None  # ticker | exchange | both
    figi: str | None = None
    composite_figi: str | None = None
    ticker: str | None = None  # TYZ6 Comdty
    name: str | None = None
    bloomberg_root: str | None = None


def live_ticker(root: str, month: date) -> str:
    """Bloomberg's ticker while a contract trades: TYZ6."""
    return f"{root}{MONTH_CODES[month.month - 1]}{month.year % 10}"


def parse_ticker(ticker: str | None) -> tuple[str, str, str] | None:
    """(root, month code, year digits) from a futures ticker, `TYZ6` or `TYZ6 Comdty`; None if it isn't one."""
    m = TICKER.match((ticker or "").split(" ")[0].upper())
    return (m["root"], m["code"], m["year"]) if m else None


def _is(row: dict, root: str, month: date) -> bool:
    """Whether OpenFIGI's row is our contract: same root, month and year (one or two digits)."""
    p = parse_ticker(row.get("ticker"))
    if p is None:
        return False
    r, code, year = p
    return r == root and code == MONTH_CODES[month.month - 1] and int(year) == month.year % 10 ** len(year)


def jobs(root: str, cme_symbol: str, month: date) -> list[dict]:
    """The two mapping jobs for a contract: by our ticker, and by CME's code."""
    return [{"idType": "TICKER", "idValue": live_ticker(root, month), "securityType2": "Future"},
            {"idType": "ID_EXCH_SYMBOL", "idValue": cme_symbol, "securityType2": "Future"}]


def _rows(item) -> list[dict]:
    return [r for r in (item.get("data") or []) if isinstance(r, dict)] if isinstance(item, dict) else []


def _failed(item) -> bool:
    """OpenFIGI answered with an error, not data or "No identifier found"."""
    if not isinstance(item, dict):
        return True
    if item.get("data"):
        return False
    return "error" in item or "No identifier found" not in str(item.get("warning", ""))


def _verdict(row: dict, via: str) -> Verdict:
    sector = row.get("marketSector")
    ticker = f"{row['ticker']} {sector}" if sector else row["ticker"]
    return Verdict("confirmed", via, row.get("figi"), row.get("compositeFIGI"), ticker, row.get("name"))


def judge(root: str, month: date, by_ticker, by_exchange) -> Verdict:
    """The contract's outcome from OpenFIGI's two answers."""
    t_rows = [r for r in _rows(by_ticker) if _is(r, root, month)]
    x_rows = _rows(by_exchange)
    x_ours = [r for r in x_rows if _is(r, root, month)]
    if x_ours:
        both = [r for r in x_ours if r.get("figi") in {t.get("figi") for t in t_rows}]
        return _verdict((both or x_ours)[0], "both" if both else "exchange")
    if x_rows:
        roots = sorted({p[0] for r in x_rows if (p := parse_ticker(r.get("ticker")))})
        return Verdict("mismatch", bloomberg_root=",".join(roots)[:8] or None,
                       name="; ".join(str(r.get("name")) for r in x_rows[:3]))
    if t_rows:
        return _verdict(t_rows[0], "ticker")
    if _failed(by_ticker) or _failed(by_exchange):
        return Verdict("error")
    return Verdict("not_found")


def _set_ticker(s: Session, sec_id: int, value: str, valid: tuple[date | None, date | None], now: datetime) -> str:
    """Bring the contract's TICKER identifier to (value, validity): added, same, or conflict (another
    instrument holds that ticker from the same day)."""
    frm, to = valid
    owner = s.scalar(select(Identifier).where(Identifier.scheme == "TICKER", Identifier.value == value,
                                              Identifier.valid_from == frm if frm else Identifier.valid_from.is_(None),
                                              Identifier.removed_at.is_(None)))
    if owner is not None and owner.sec_id != sec_id:
        return "conflict"
    changed = False
    for r in s.scalars(select(Identifier).where(Identifier.scheme == "TICKER", Identifier.sec_id == sec_id,
                                                Identifier.removed_at.is_(None))):
        if r is owner:
            continue
        r.removed_at = now
        changed = True
    if owner is None:
        s.flush()
        s.add(Identifier(sec_id=sec_id, scheme="TICKER", value=value, valid_from=frm, valid_to=to, created_at=now))
        return "added"
    if owner.valid_to != to:
        owner.valid_to = to
        changed = True
    return "changed" if changed else "same"


def _cme_validity(s: Session, sec_ids: list[int]) -> dict[int, tuple[date | None, date | None]]:
    """Each contract's current CME code validity (the latest, when its code has come round before)."""
    out: dict[int, tuple[date | None, date | None]] = {}
    for r in s.scalars(select(Identifier).where(Identifier.scheme == "CME", Identifier.sec_id.in_(sec_ids),
                                                Identifier.removed_at.is_(None)).order_by(Identifier.valid_from)):
        out[r.sec_id] = (r.valid_from, r.valid_to)
    return out


def run(s: Session, api_key: str | None, now: datetime | None = None, mapper=None) -> dict:
    """Ask OpenFIGI about listed contracts not yet confirmed under their product's root; keep the answers."""
    now = now or datetime.now(UTC)
    mapper = mapper or figi.map_jobs
    live = s.execute(
        select(FuturesContract.sec_id, FuturesContract.product_sec_id, FuturesContract.contract_month,
               FuturesProduct.root, FuturesProduct.cme_code)
        .join(FuturesProduct, FuturesProduct.sec_id == FuturesContract.product_sec_id)
        .where(FuturesContract.superseded_at.is_(None), FuturesContract.status.in_(LIVE))
        .order_by(FuturesProduct.root, FuturesContract.contract_month)).all()
    seen = {r.sec_id: r for r in s.scalars(select(FuturesFigiLookup))}
    names = dict(s.execute(select(InstrumentName.sec_id, InstrumentName.name).where(
        InstrumentName.kind == "short", InstrumentName.removed_at.is_(None),
        InstrumentName.sec_id.in_([c.sec_id for c in live]))).all()) if live else {}
    todo = [c for c in live
            if c.sec_id not in seen or seen[c.sec_id].outcome != "confirmed" or seen[c.sec_id].root != c.root]
    batch = todo if api_key else todo[:MAX_WITHOUT_KEY]
    out = {"asked": len(batch), "left": len(todo) - len(batch), "with_key": bool(api_key),
           "confirmed": 0, "mismatch": 0, "not_found": 0, "error": 0,
           "tickers_added": 0, "tickers_changed": 0, "conflicts": [], "errors": []}
    answers = mapper([j for c in batch for j in jobs(c.root, f"{c.cme_code}{MONTH_CODES[c.contract_month.month - 1]}"
                                                     f"{c.contract_month.year % 10}", c.contract_month)],
                     api_key) if batch else []
    for i, c in enumerate(batch):
        by_ticker, by_exchange = answers[2 * i], answers[2 * i + 1]
        v = judge(c.root, c.contract_month, by_ticker, by_exchange)
        row = seen.get(c.sec_id) or FuturesFigiLookup(sec_id=c.sec_id)
        row.product_sec_id, row.root, row.outcome, row.via = c.product_sec_id, c.root, v.outcome, v.via
        row.figi, row.composite_figi, row.ticker, row.bloomberg_root = v.figi, v.composite_figi, v.ticker, v.bloomberg_root
        row.detail = json.loads(json.dumps({"by_ticker": by_ticker, "by_exchange": by_exchange, "name": v.name}))
        row.looked_up_at = now
        seen[c.sec_id] = s.merge(row)
        out[v.outcome] += 1
        if v.outcome == "error":
            out["errors"].append({"contract": names.get(c.sec_id), "by_ticker": by_ticker, "by_exchange": by_exchange})
        if v.outcome == "confirmed":
            _soft_identifier(s, c.sec_id, "FIGI", v.figi)
            if v.composite_figi and v.composite_figi != v.figi:
                _soft_identifier(s, c.sec_id, "COMPOSITE-FIGI", v.composite_figi)
    s.flush()

    # Tickers for every confirmed listed contract, valid while its CME code is.
    valid = _cme_validity(s, [c.sec_id for c in live]) if live else {}
    for c in live:
        row = seen.get(c.sec_id)
        if row is None or row.outcome != "confirmed" or row.root != c.root or not row.ticker:
            continue
        got = _set_ticker(s, c.sec_id, row.ticker, valid.get(c.sec_id, (None, None)), now)
        if got == "conflict":
            out["conflicts"].append({"contract": names.get(c.sec_id), "ticker": row.ticker})
        elif got in ("added", "changed"):
            out[f"tickers_{got}"] += 1
    s.commit()

    products: dict[str, dict] = {}
    for c in live:
        p = products.setdefault(c.root, {"listed": 0, "confirmed": 0, "via": {}, "mismatch": 0,
                                         "bloomberg_roots": [], "not_found": 0, "error": 0, "unasked": 0})
        p["listed"] += 1
        row = seen.get(c.sec_id)
        if row is None or row.root != c.root:
            p["unasked"] += 1
            continue
        p[row.outcome] += 1
        if row.outcome == "confirmed":
            p["via"][row.via] = p["via"].get(row.via, 0) + 1
            p.setdefault("example", {"contract": names.get(c.sec_id), "ticker": row.ticker,
                                     "name": (row.detail or {}).get("name"), "figi": row.figi})
        if row.outcome == "mismatch" and row.bloomberg_root and row.bloomberg_root not in p["bloomberg_roots"]:
            p["bloomberg_roots"].append(row.bloomberg_root)
    out["products"] = products
    out["roots_confirmed"] = sorted(r for r, p in products.items() if p["confirmed"] == p["listed"])
    out["conflicts"], out["errors"] = out["conflicts"][:20], out["errors"][:20]
    return out
