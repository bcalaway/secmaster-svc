"""Generate and store futures products and contracts (mkt-data's docs/phase-4.md, step 2a).

Reads seeds/futures.toml, fetches every calendar its rules count in from calendar-svc (once each),
generates each product's contracts (app/futures.py) and brings the security master in line:

- **Products** are instruments of type `fut_product`, short-named by their Bloomberg root (`TY`),
  identified by CME's code (scheme `CME`, `ZN`), and found again by that code, so a root change
  renames the product and its contracts and keeps their sec_ids. Specs are futures_spec rows,
  superseded (never edited) when the seed changes them.
- **Contracts** are instruments of type `fut_treasury`, `fut_stir` or `fut_fx`, one per product and
  contract month, short-named `TYZ26`, with CME's code (`ZNZ6`, scheme `CME`) valid from the day
  it's listed (or the day after the same code last expired, ten years before, when the listing day
  isn't known) to its last trading day. Their dates are futures_contract rows, superseded when a
  date or rule changes. A first trading day, once derived, is kept after the contract expires.
- **Generics** (`TY1`, `TY2`) are identifiers (scheme `GENERIC`) with validity, like on-the-run.
- **Baskets** (step 3): each Treasury contract listed or in delivery gets its deliverable securities and
  conversion factors (app/baskets.py) from phase 3's terms, as futures_deliverable rows, superseded when
  a factor or the rule changes and closed when a security leaves the basket. Expired contracts keep
  their last basket.

Idempotent: a second run on the same day changes nothing. A stored contract the rules stop generating
is counted and gets a status of its own: `expired` once its dates have passed (a serial month, or a
product kept to today's listings, drops out of generation when it stops trading), else `withdrawn` (a
corrected rule doesn't list it: its CME code, generics and ticker are retired, and it comes back if the
cycle lists it later). Its dates are kept. Each run is recorded in futures_run.
"""

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import baskets, calendars, futures, futures_seed
from app.models import (
    FuturesContract,
    FuturesDeliverable,
    FuturesProduct,
    FuturesRun,
    FuturesSpec,
    Identifier,
    Instrument,
    InstrumentName,
    SecurityTerms,
)

CME = "CME"
GENERIC = "GENERIC"
FIRST_YEAR = date(1990, 1, 1)
HORIZON_YEARS = 15
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


class FuturesError(RuntimeError):
    pass


def _instrument_status(status: str) -> str:
    return status if status in ("expired", "withdrawn") else "active"


def _stored_status(row: FuturesContract, kind: str, today: date) -> str:
    """The status of a stored contract the rules no longer generate: expired once its dates have passed
    (a serial month, or a product kept to today's listings, drops out of generation when it stops trading),
    otherwise withdrawn (a corrected rule doesn't list it; it comes back if the cycle lists it later)."""
    end = row.last_delivery_date if kind == "treasury" and row.last_delivery_date else row.last_trade_date
    return "expired" if today > end else "withdrawn"


_COPY = ("first_trade_date", "last_trade_date", "first_intention_date", "first_notice_date", "first_delivery_date",
         "last_delivery_date", "reference_start", "reference_end", "final_settlement_date", "settlement_date", "rules")


class _Names:
    """Short names and aliases, loaded once: a name belongs to one instrument for good."""

    def __init__(self, s: Session, now: datetime):
        self.s, self.now = s, now
        self.rows = {r.name: r for r in s.scalars(select(InstrumentName))}
        self.short = {r.sec_id: r for r in self.rows.values() if r.kind == "short" and r.removed_at is None}
        self.changed = 0

    def set_short(self, sec_id: int, name: str) -> None:
        row = self.rows.get(name)
        if row is not None and row.sec_id != sec_id:
            raise FuturesError(f"name {name} belongs to sec_id {row.sec_id}")
        current = self.short.get(sec_id)
        if current is not None and current.name == name:
            return
        if current is not None:
            current.kind = "alias"  # a rename keeps the old name as an alias
            self.s.flush()
        if row is None:
            row = InstrumentName(sec_id=sec_id, name=name, kind="short", created_at=self.now)
            self.s.add(row)
            self.rows[name] = row
        else:
            row.kind, row.removed_at = "short", None
        self.short[sec_id] = row
        self.changed += 1


def _sync_specs(s: Session, sec_id: int, specs, now: datetime) -> int:
    current = list(s.scalars(select(FuturesSpec).where(FuturesSpec.sec_id == sec_id,
                                                       FuturesSpec.superseded_at.is_(None))))
    have = {(r.valid_from, r.valid_to, r.source, json.dumps(r.fields, sort_keys=True)): r for r in current}
    want = {(x.valid_from, x.valid_to, x.source, json.dumps(x.fields, sort_keys=True)): x for x in specs}
    changed = 0
    for key, row in have.items():
        if key not in want:
            row.superseded_at = now
            changed += 1
    for key, x in want.items():
        if key not in have:
            s.add(FuturesSpec(sec_id=sec_id, valid_from=x.valid_from, valid_to=x.valid_to, source=x.source,
                              fields=x.fields, recorded_at=now))
            changed += 1
    return changed


def _product(s: Session, ps: futures_seed.ProductSeed, seed_sha: str, names: _Names, now: datetime,
             out: dict) -> int:
    p = ps.product
    row = s.scalar(select(FuturesProduct).where(FuturesProduct.cme_code == p.cme_code))
    attrs = {"type": "fut_product", "currency": ps.currency, "country": "US", "curve": None, "tenor": None,
             "calendar": p.trade_calendars[0], "description": ps.name}
    if row is None:
        inst = Instrument(**attrs, status="active", created_at=now, updated_at=now)
        s.add(inst)
        s.flush()
        row = FuturesProduct(sec_id=inst.sec_id, root=p.root, cme_code=p.cme_code, kind=p.kind, info=ps.info,
                             seed_sha256=seed_sha, updated_at=now)
        s.add(row)
        out["products_created"] += 1
    else:
        inst = s.get(Instrument, row.sec_id)
        if (row.root, row.kind, row.info) != (p.root, p.kind, ps.info) or any(
                getattr(inst, k) != v for k, v in attrs.items()):
            row.root, row.kind, row.info, row.seed_sha256, row.updated_at = p.root, p.kind, ps.info, seed_sha, now
            for k, v in attrs.items():
                setattr(inst, k, v)
            inst.updated_at = now
            out["products_updated"] += 1
    names.set_short(row.sec_id, p.root)
    if s.scalar(select(Identifier.id).where(Identifier.sec_id == row.sec_id, Identifier.scheme == CME,
                                            Identifier.value == p.cme_code, Identifier.removed_at.is_(None))) is None:
        s.add(Identifier(sec_id=row.sec_id, scheme=CME, value=p.cme_code, created_at=now))
    out["specs_changed"] += _sync_specs(s, row.sec_id, ps.specs, now)
    return row.sec_id


def _description(ps: futures_seed.ProductSeed, month: date) -> str:
    return f"{ps.name}, {MONTHS[month.month - 1]} {month.year}"


def _contract_values(d: futures.Dated, previous: FuturesContract | None) -> dict:
    values = {f: d.dates[f] for f in futures.DATE_FIELDS}
    rules = dict(d.rules)
    if values["first_trade_date"] is None and previous is not None and previous.first_trade_date is not None:
        values["first_trade_date"] = previous.first_trade_date  # derived while it was listed; still true
        rules["first_trade_date"] = previous.rules.get("first_trade_date", "")
    return values | {"status": d.status, "rules": rules}


def _same(row: FuturesContract, values: dict) -> bool:
    return all(getattr(row, k) == v for k, v in values.items())


def _symbol_validity(contracts: list[tuple[futures.Dated, dict]]) -> dict[int, tuple[date, date]]:
    """CME's code is valid from the listing day (or the day after the same code last expired) to the last
    trading day. Keyed by id() of the Dated."""
    out, last_seen = {}, {}
    for d, values in sorted(contracts, key=lambda x: x[0].contract.month):
        sym, ltd = d.contract.cme_symbol, d.last_trade
        frm = values["first_trade_date"]
        if frm is None:
            frm = last_seen[sym] + timedelta(days=1) if sym in last_seen else date(ltd.year - 10, ltd.month, 1)
        out[id(d)] = (frm, ltd)
        last_seen[sym] = ltd
    return out


def _sync_identifiers(s: Session, scheme: str, want: dict[tuple[str, date], tuple[int, date | None]],
                      now: datetime, sec_ids: set[int] = frozenset(), values: set[str] = frozenset()
                      ) -> tuple[int, int]:
    """Bring a scheme's current identifiers on these instruments, or with these values (and the wanted
    ones), in line with want: (value, valid_from) -> (sec_id, valid_to)."""
    values = set(values) | {v for v, _ in want}
    cond = Identifier.value.in_(sorted(values))
    if sec_ids:
        cond = or_(cond, Identifier.sec_id.in_(sorted(sec_ids)))
    rows = list(s.scalars(select(Identifier).where(Identifier.scheme == scheme, Identifier.removed_at.is_(None),
                                                   cond)))
    added = removed = 0
    have = {}
    for r in rows:
        w = want.get((r.value, r.valid_from))
        if w is None or w[0] != r.sec_id:
            r.removed_at = now
            removed += 1
        else:
            if r.valid_to != w[1]:
                r.valid_to = w[1]
            have[(r.value, r.valid_from)] = r
    s.flush()
    for (value, frm), (sec_id, to) in sorted(want.items()):
        if (value, frm) not in have:
            s.add(Identifier(sec_id=sec_id, scheme=scheme, value=value, valid_from=frm, valid_to=to, created_at=now))
            added += 1
    return added, removed


def _contracts(s: Session, ps: futures_seed.ProductSeed, product_sec_id: int, gen: futures.Generated,
               names: _Names, now: datetime, out: dict, today: date) -> None:
    p = ps.product
    rows = {r.contract_month: r for r in s.scalars(select(FuturesContract).where(
        FuturesContract.product_sec_id == product_sec_id, FuturesContract.superseded_at.is_(None)))}
    existing = {i.sec_id: i for i in s.scalars(select(Instrument).where(
        Instrument.sec_id.in_([r.sec_id for r in rows.values()])))} if rows else {}
    new = [d for d in gen.contracts if d.contract.month not in rows]
    insts = {}
    for d in new:
        inst = Instrument(type=futures.TYPES[p.kind], currency=ps.currency, country="US", curve=None, tenor=None,
                          calendar=p.trade_calendars[0], status=_instrument_status(d.status),
                          description=_description(ps, d.contract.month), created_at=now, updated_at=now)
        s.add(inst)
        insts[d.contract.month] = inst
    s.flush()
    out["contracts_created"] += len(new)

    by_sec: dict[int, futures.Dated] = {}
    valued = []
    for d in gen.contracts:
        m = d.contract.month
        previous = rows.get(m)
        values = _contract_values(d, previous)
        if previous is None:
            sec_id = insts[m].sec_id
            s.add(FuturesContract(sec_id=sec_id, product_sec_id=product_sec_id, contract_month=m, recorded_at=now,
                                  **values))
        else:
            sec_id = previous.sec_id
            if not _same(previous, values):
                previous.superseded_at = now
                s.flush()
                s.add(FuturesContract(sec_id=sec_id, product_sec_id=product_sec_id, contract_month=m,
                                      recorded_at=now, **values))
                out["contracts_changed"] += 1
            inst = existing[sec_id]
            want = (_instrument_status(d.status), _description(ps, m), futures.TYPES[p.kind], p.trade_calendars[0])
            if (inst.status, inst.description, inst.type, inst.calendar) != want:
                inst.status, inst.description, inst.type, inst.calendar = want
                inst.updated_at = now
        names.set_short(sec_id, d.contract.short_name)
        by_sec[sec_id] = d
        valued.append((d, values))
    generated = {d.contract.month for d in gen.contracts}
    out["contracts_not_generated"] += len(set(rows) - generated)
    withdrawn: set[int] = set()
    for m, row in rows.items():
        if m in generated:
            continue
        status = _stored_status(row, p.kind, today)
        if status == "withdrawn":
            withdrawn.add(row.sec_id)
        if row.status != status:
            row.superseded_at = now
            s.flush()
            s.add(FuturesContract(sec_id=row.sec_id, product_sec_id=product_sec_id, contract_month=m,
                                  recorded_at=now, status=status, **{k: getattr(row, k) for k in _COPY}))
            inst = existing[row.sec_id]
            inst.status, inst.updated_at = _instrument_status(status), now
            out[f"contracts_{status}"] += 1
    s.flush()
    if withdrawn:
        # A withdrawn contract keeps its names and FIGI, but nothing that says it trades.
        for r in s.scalars(select(Identifier).where(Identifier.sec_id.in_(sorted(withdrawn)),
                                                    Identifier.scheme.in_((CME, GENERIC, "TICKER")),
                                                    Identifier.removed_at.is_(None))):
            r.removed_at = now
            out["cme_codes_removed" if r.scheme == CME else "generics_removed" if r.scheme == GENERIC
                else "tickers_removed"] += 1
        s.flush()

    sec_of = {id(d): sec for sec, d in by_sec.items()}
    validity = _symbol_validity(valued)
    want = {(d.contract.cme_symbol, validity[id(d)][0]): (sec_of[id(d)], validity[id(d)][1]) for d, _ in valued}
    a, r = _sync_identifiers(s, CME, want, now, sec_ids=set(by_sec))
    out["cme_codes_added"] += a
    out["cme_codes_removed"] += r

    sec_by_month = {d.contract.month: sec for sec, d in by_sec.items()}
    gwant = {(g.alias, g.valid_from): (sec_by_month[g.contract.month], g.valid_to) for g in gen.generics}
    # Every generic on the product's contracts, so a smaller `generics` or a new root retires the old ones.
    a, r = _sync_identifiers(s, GENERIC, gwant, now, sec_ids=set(by_sec))
    out["generics_added"] += a
    out["generics_removed"] += r


def _securities(s: Session) -> list[baskets.Security]:
    rows = s.scalars(select(SecurityTerms).where(SecurityTerms.superseded_at.is_(None),
                                                 SecurityTerms.security_type.in_(("note", "bond"))))
    return [baskets.Security(r.sec_id, r.cusip, r.security_type, r.coupon_rate, r.coupon_frequency,
                             r.issue_date, r.maturity_date, r.call_date if r.callable else None) for r in rows]


def _baskets(s: Session, ps: futures_seed.ProductSeed, product_sec_id: int, securities: list[baskets.Security],
             now: datetime, out: dict) -> None:
    rule = ps.basket
    contracts = s.scalars(select(FuturesContract).where(
        FuturesContract.product_sec_id == product_sec_id, FuturesContract.superseded_at.is_(None),
        FuturesContract.status.in_(("listed", "delivery"))))
    for c in contracts:
        if c.contract_month < baskets.SIX_PERCENT_FROM or c.last_delivery_date is None:
            continue
        want = {d.sec_id: d for d in baskets.basket(rule, securities, c.contract_month, c.last_delivery_date)}
        have = {r.security_sec_id: r for r in s.scalars(select(FuturesDeliverable).where(
            FuturesDeliverable.contract_sec_id == c.sec_id, FuturesDeliverable.superseded_at.is_(None)))}
        for sec_id, r in have.items():
            d = want.get(sec_id)
            if d is None:
                r.superseded_at = now
                out["deliverables_removed"] += 1
            elif (r.conversion_factor, r.remaining_months, r.valid_from, r.rule) != (
                    d.conversion_factor, d.remaining_months, d.valid_from, rule.text):
                r.superseded_at = now
                out["deliverables_changed"] += 1
        s.flush()
        for sec_id, d in want.items():
            r = have.get(sec_id)
            if r is not None and r.superseded_at is None:
                continue
            if r is None:
                out["deliverables_added"] += 1
            s.add(FuturesDeliverable(contract_sec_id=c.sec_id, security_sec_id=sec_id,
                                     conversion_factor=d.conversion_factor, remaining_months=d.remaining_months,
                                     valid_from=d.valid_from, rule=rule.text, recorded_at=now))
    s.flush()


def _apply(s: Session, seed: futures_seed.FuturesSeed, cals: futures.Calendars, today: date, now: datetime) -> dict:
    out = dict.fromkeys(("products_created", "products_updated", "specs_changed", "contracts_created",
                         "contracts_changed", "contracts_not_generated", "cme_codes_added", "cme_codes_removed",
                         "generics_added", "generics_removed", "contracts_expired", "contracts_withdrawn",
                         "tickers_removed", "deliverables_added", "deliverables_changed", "deliverables_removed"), 0)
    names = _Names(s, now)
    products = {}
    securities = _securities(s) if any(ps.basket for ps in seed.products) else []
    for ps in seed.products:
        sec_id = _product(s, ps, seed.sha256, names, now, out)
        gen = futures.generate(ps.product, cals, today, HORIZON_YEARS)
        _contracts(s, ps, sec_id, gen, names, now, out, today)
        if ps.basket:
            _baskets(s, ps, sec_id, securities, now, out)
        listed = [d for d in gen.contracts if d.listed_today]
        products[ps.product.root] = {"contracts": len(gen.contracts), "listed": len(listed),
                                     "front": listed[0].contract.short_name if listed else None}
    out["names_changed"] = names.changed
    out["changed"] = any(v for k, v in out.items() if k != "contracts_not_generated")
    out["products"] = products
    return out


def run(s: Session, source: calendars.CalendarSource, now: datetime | None = None, today: date | None = None,
        seed: futures_seed.FuturesSeed | None = None) -> dict:
    """One generation run: seed, calendars, contracts. Commits; raises FuturesError (recorded) on failure."""
    from app.securities import today_ny

    now = now or datetime.now(UTC)
    today = today or today_ny()
    sha = ""
    try:
        seed = seed or futures_seed.load()
        sha = seed.sha256
        names = sorted({c for ps in seed.products for c in ps.product.calendars})
        end = date(today.year + HORIZON_YEARS + 2, 12, 31)
        cals = calendars.fetch(source, names, FIRST_YEAR, end)
        out = _apply(s, seed, cals, today, now)
    except (futures_seed.FuturesSeedError, calendars.CalendarUnavailable, futures.CalendarError,
            futures.RuleError, FuturesError) as e:
        s.rollback()
        s.add(FuturesRun(started_at=now, finished_at=datetime.now(UTC), outcome="error", seed_sha256=sha,
                         detail=json.dumps({"error": str(e)})))
        s.commit()
        raise FuturesError(str(e)) from None
    summary = {"today": today.isoformat(), "seed_sha256": sha} | out
    s.add(FuturesRun(started_at=now, finished_at=datetime.now(UTC), outcome="ok", seed_sha256=sha,
                     detail=json.dumps(summary)))
    s.commit()
    return summary
