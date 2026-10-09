"""FIGIs, Bloomberg tickers and the Bloomberg roots for listed futures contracts, from OpenFIGI
(mkt-data's docs/phase-4.md, step 2b).

Each listed contract (status listed or delivery) is asked three questions in one mapping request:

- **By ticker:** our Bloomberg-style ticker while it trades (`TYZ6`, idType TICKER, securityType2
  Future), and the same with a two-digit year (`SFRZ29`) for contracts whose one-digit ticker an
  expired contract may still hold. Both are asked in the product's market sector: `Curncy` for FX,
  `Comdty` for rates. Without it a ticker can find another product's future: BPV6, ECV6 and NVZ6
  are soybean, crude and heating oil futures under Comdty (the first run, 2026-10-08).
- **By CME's code:** CME's symbol (`ZNZ6`, idType ID_EXCH_SYMBOL, securityType2 Future, which
  OpenFIGI requires with that idType). Its answer carries Bloomberg's ticker for the contract, so
  it confirms the root independently, or names the one Bloomberg uses. (On the first run it found
  nothing for any contract; the job's answer shows what OpenFIGI said, per product.)

The answers decide the contract's outcome:

- `confirmed`: CME's code maps to our ticker (via `exchange`, or `both` when the ticker question
  found it too), or only the ticker question found it in the product's sector (via `ticker`;
  weaker, since nothing ties that future to CME's code). The job's answer shows each product's
  future by name, and a person checks those before the seed's `root_confirmed` is set.
- `mismatch`: CME's code maps to a future with another root (`bloomberg_root`); nothing is stored,
  and the seed's root is the thing to fix (in a reviewed PR).
- `not_found` or `error` (OpenFIGI's own words kept in `detail`); asked again next run.

For a confirmed contract the job keeps its FIGI (scheme FIGI), composite FIGI when it differs
(COMPOSITE-FIGI) and Bloomberg's live ticker with its market sector (`TYZ6 Comdty`, scheme TICKER),
valid while the contract is listed, the same as its CME code. A confirmed contract isn't asked
again unless its product's root or these checks change (CHECK); its ticker's validity follows
the CME code's each run. When a contract asked again isn't confirmed, or is confirmed as another
future, the FIGI, composite FIGI and ticker this job gave it are retired.

The job's answer lists, per product, how many of its listed contracts each outcome covers and the
roots Bloomberg used, an example future, every matched future's name (without its month) with its
sector and exchange and how many contracts it covers, and what OpenFIGI said to the CME-code question. Runs after the futures
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
# The version of the questions and checks below: a lookup made under an older one is asked again.
# 1: no market sector (2026-10-08, matched three FX products to commodity futures); 2: sector by kind.
CHECK = 2
SECTOR = {"fx": "Curncy", "fx_cash": "Curncy", "treasury": "Comdty", "treasury_cash": "Comdty", "stir": "Comdty"}
# Without a key OpenFIGI takes 25 requests a minute of 10 jobs; three jobs a contract.
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


def jobs(root: str, cme_symbol: str, month: date, kind: str) -> list[dict]:
    """The three mapping jobs for a contract: by our ticker (one- and two-digit year, in the product's
    sector), and by CME's code."""
    sector = SECTOR[kind]
    two = f"{root}{MONTH_CODES[month.month - 1]}{month.year % 100:02d}"
    return [{"idType": "TICKER", "idValue": live_ticker(root, month), "marketSecDes": sector, "securityType2": "Future"},
            {"idType": "TICKER", "idValue": two, "marketSecDes": sector, "securityType2": "Future"},
            {"idType": "ID_EXCH_SYMBOL", "idValue": cme_symbol, "securityType2": "Future"}]


def said(item) -> str:
    """OpenFIGI's answer in a few words: data (n), or its warning or error."""
    if not isinstance(item, dict):
        return f"unexpected: {str(item)[:60]}"
    if item.get("data"):
        return f"data ({len(item['data'])})"
    return str(item.get("error") or item.get("warning") or "empty")[:80]


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


def judge(root: str, month: date, by_ticker, by_exchange, sector: str | None = None) -> Verdict:
    """The contract's outcome from OpenFIGI's answers (by_ticker: one answer or a list of them). A ticker
    answer counts only in the product's sector, when given."""
    tickers = by_ticker if isinstance(by_ticker, list) else [by_ticker]
    t_rows = [r for item in tickers for r in _rows(item) if _is(r, root, month)
              and (sector is None or r.get("marketSector") == sector)]
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
    if any(_failed(i) for i in tickers) or _failed(by_exchange):
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


MONTH_YEAR = re.compile(r"\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{2}$")


def product_name(name: str | None) -> str:
    """A future's name without its contract month: "US 10YR NOTE (CBT)Dec26" -> "US 10YR NOTE (CBT)"."""
    return MONTH_YEAR.sub("", (name or "").strip()).strip() or "?"


def _cme_symbol(c) -> str:
    return f"{c.cme_code}{MONTH_CODES[c.contract_month.month - 1]}{c.contract_month.year % 10}"


def _exch_code(row: FuturesFigiLookup) -> str | None:
    """The exchange code on the future OpenFIGI matched (CBT, CME), from the answer kept in detail."""
    d = row.detail or {}
    items = d.get("by_ticker") or []
    for item in [*(items if isinstance(items, list) else [items]), d.get("by_exchange")]:
        for r in _rows(item):
            if r.get("figi") == row.figi:
                return r.get("exchCode")
    return None


def _retire(s: Session, sec_id: int, old: FuturesFigiLookup, now: datetime) -> None:
    """Retire the FIGI, composite FIGI and ticker an earlier lookup gave this contract."""
    for scheme, value in (("FIGI", old.figi), ("COMPOSITE-FIGI", old.composite_figi), ("TICKER", old.ticker)):
        if not value:
            continue
        for r in s.scalars(select(Identifier).where(Identifier.sec_id == sec_id, Identifier.scheme == scheme,
                                                    Identifier.value == value, Identifier.removed_at.is_(None))):
            r.removed_at = now
    s.flush()


def run(s: Session, api_key: str | None, now: datetime | None = None, mapper=None) -> dict:
    """Ask OpenFIGI about listed contracts not yet confirmed under their product's root; keep the answers."""
    now = now or datetime.now(UTC)
    mapper = mapper or figi.map_jobs
    live = s.execute(
        select(FuturesContract.sec_id, FuturesContract.product_sec_id, FuturesContract.contract_month,
               FuturesProduct.root, FuturesProduct.cme_code, FuturesProduct.kind)
        .join(FuturesProduct, FuturesProduct.sec_id == FuturesContract.product_sec_id)
        .where(FuturesContract.superseded_at.is_(None), FuturesContract.status.in_(LIVE))
        .order_by(FuturesProduct.root, FuturesContract.contract_month)).all()
    seen = {r.sec_id: r for r in s.scalars(select(FuturesFigiLookup))}
    names = dict(s.execute(select(InstrumentName.sec_id, InstrumentName.name).where(
        InstrumentName.kind == "short", InstrumentName.removed_at.is_(None),
        InstrumentName.sec_id.in_([c.sec_id for c in live]))).all()) if live else {}
    todo = [c for c in live if c.sec_id not in seen or seen[c.sec_id].outcome != "confirmed"
            or seen[c.sec_id].root != c.root or (seen[c.sec_id].detail or {}).get("check") != CHECK]
    batch = todo if api_key else todo[:MAX_WITHOUT_KEY]
    out = {"asked": len(batch), "left": len(todo) - len(batch), "with_key": bool(api_key),
           "confirmed": 0, "mismatch": 0, "not_found": 0, "error": 0,
           "tickers_added": 0, "tickers_changed": 0, "retired": [], "conflicts": [], "errors": []}
    n = len(jobs("X", "X", date(2000, 1, 1), "fx"))
    answers = mapper([j for c in batch for j in jobs(c.root, _cme_symbol(c), c.contract_month, c.kind)],
                     api_key) if batch else []
    exchange_said: dict[str, dict[str, int]] = {}
    first_missing: dict[str, dict] = {}
    for i, c in enumerate(batch):
        by_ticker, by_exchange = answers[n * i:n * i + n - 1], answers[n * i + n - 1]
        v = judge(c.root, c.contract_month, by_ticker, by_exchange, SECTOR[c.kind])
        counts = exchange_said.setdefault(c.root, {})
        counts[said(by_exchange)] = counts.get(said(by_exchange), 0) + 1
        if v.outcome == "not_found" and c.root not in first_missing:
            first_missing[c.root] = {"contract": names.get(c.sec_id),
                                     "said": [f"{j['idValue']}: {said(a)}" for j, a in zip(
                                         jobs(c.root, _cme_symbol(c), c.contract_month, c.kind),
                                         [*by_ticker, by_exchange], strict=True)]}
        old = seen.get(c.sec_id)
        if old is not None and old.outcome == "confirmed" and (v.outcome != "confirmed" or v.figi != old.figi):
            out["retired"].append({"contract": names.get(c.sec_id), "figi": old.figi, "ticker": old.ticker,
                                   "now": v.outcome})
            _retire(s, c.sec_id, old, now)
        row = old or FuturesFigiLookup(sec_id=c.sec_id)
        row.product_sec_id, row.root, row.outcome, row.via = c.product_sec_id, c.root, v.outcome, v.via
        row.figi, row.composite_figi, row.ticker, row.bloomberg_root = v.figi, v.composite_figi, v.ticker, v.bloomberg_root
        row.detail = json.loads(json.dumps({"check": CHECK, "by_ticker": by_ticker, "by_exchange": by_exchange,
                                            "name": v.name}))
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
                                         "bloomberg_roots": [], "not_found": 0, "error": 0, "unasked": 0,
                                         "names": {}})
        p["listed"] += 1
        row = seen.get(c.sec_id)
        if row is None or row.root != c.root:
            p["unasked"] += 1
            continue
        p[row.outcome] += 1
        if row.outcome == "confirmed":
            p["via"][row.via] = p["via"].get(row.via, 0) + 1
            p.setdefault("example", {"contract": names.get(c.sec_id), "ticker": row.ticker,
                                     "name": (row.detail or {}).get("name"), "figi": row.figi,
                                     "exch_code": _exch_code(row)})
            # Every matched future's name without its month, and its sector and exchange: one entry per
            # product when every contract is the same future, more when some matched another.
            key = f"{product_name((row.detail or {}).get('name'))} | {(row.ticker or '').split(' ')[-1]} | " \
                  f"{_exch_code(row)}"
            p["names"][key] = p["names"].get(key, 0) + 1
        if row.outcome == "mismatch" and row.bloomberg_root and row.bloomberg_root not in p["bloomberg_roots"]:
            p["bloomberg_roots"].append(row.bloomberg_root)
    for root, counts in exchange_said.items():
        products[root]["exchange_said"] = counts
    for root, missing in first_missing.items():
        products[root]["first_not_found"] = missing
    out["products"] = products
    # Every listed contract found: a root to check by the example's name, not one the job confirms by itself.
    out["roots_found"] = sorted(r for r, p in products.items() if p["confirmed"] == p["listed"])
    out["retired_count"], out["retired"] = len(out["retired"]), out["retired"][:20]
    out["conflicts"], out["errors"] = out["conflicts"][:20], out["errors"][:20]
    return out
