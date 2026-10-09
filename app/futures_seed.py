"""Read and check seeds/futures.toml, the futures product seed (mkt-data's docs/phase-4.md, step 2a).

It isn't an instrument seed (app/seed.py skips it): it lists products, their effective-dated specs as
CME states them, and the rules app/futures.py generates contracts from. Parsing checks everything a
generator run relies on, so a bad file fails in CI and at the job, never part way through a run.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path

from app.futures import KINDS, RULE_FIELDS, Listing, Product, RuleError, check_rule

PATH = Path(__file__).resolve().parent.parent / "seeds" / "futures.toml"
ROOT = re.compile(r"^[A-Z0-9]{2,4}$")
CODE = re.compile(r"^[A-Z0-9]{1,4}$")
CALENDAR = re.compile(r"^[A-Z][A-Z0-9\-]{1,19}$")
SPEC_FIELDS = ("contract_unit", "minimum_price_fluctuation", "listed_contracts", "termination_of_trading",
               "settlement_method", "settlement_procedures", "trading_hours", "vendor_codes")
REQUIRED_RULES = {
    "treasury": {"last_trade_date", "first_intention_date", "first_notice_date", "first_delivery_date",
                 "last_delivery_date"},
    "stir": {"last_trade_date"},
    "fx": {"last_trade_date", "settlement_date"},
}


class FuturesSeedError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    valid_from: date
    valid_to: date | None
    source: str
    fields: dict  # SPEC_FIELDS -> text as CME states it


@dataclass(frozen=True)
class ProductSeed:
    product: Product
    name: str
    clearing_code: str
    currency: str
    root_confirmed: bool
    root_source: str
    history_source: str
    rule_sources: dict
    specs: tuple[Spec, ...]

    @property
    def info(self) -> dict:
        """What's stored with the product: everything but the specs (kept as their own rows)."""
        p = self.product
        return {
            "root": p.root, "root_confirmed": self.root_confirmed, "root_source": self.root_source,
            "cme_code": p.cme_code, "clearing_code": self.clearing_code, "name": self.name, "kind": p.kind,
            "trade_calendars": list(p.trade_calendars), "settle_calendars": list(p.settle_calendars),
            "listing": {k: v for k, v in vars(p.listing).items() if v},
            "rules": dict(p.rules), "rule_sources": dict(self.rule_sources), "generics": p.generics,
            "history_from": p.history_from.isoformat() if p.history_from else None,
            "history_source": self.history_source,
        }


@dataclass(frozen=True)
class FuturesSeed:
    sha256: str
    read_on: date
    products: tuple[ProductSeed, ...]


def _date(v, where: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise FuturesSeedError(f"{where}: bad date {v!r}") from None


def _text(item: dict, key: str, where: str) -> str:
    v = item.get(key)
    if not isinstance(v, str) or not v.strip():
        raise FuturesSeedError(f"{where}: {key} is missing")
    return v


def _specs(raw: list, where: str) -> tuple[Spec, ...]:
    if not raw:
        raise FuturesSeedError(f"{where}: no specs")
    out = []
    for i, s in enumerate(raw):
        w = f"{where} spec {i + 1}"
        frm = _date(s.get("valid_from"), w)
        to = _date(s["valid_to"], w) if s.get("valid_to") else None
        if to is not None and to < frm:
            raise FuturesSeedError(f"{w}: valid_to before valid_from")
        src = _text(s, "source", w)
        if not src.startswith("https://"):
            raise FuturesSeedError(f"{w}: source must be a URL")
        out.append(Spec(frm, to, src, {k: _text(s, k, w) for k in SPEC_FIELDS}))
    out.sort(key=lambda x: x.valid_from)
    for a, b in pairwise(out):
        if a.valid_to is None or a.valid_to >= b.valid_from:
            raise FuturesSeedError(f"{where}: specs overlap ({a.valid_from} and {b.valid_from})")
    return tuple(out)


def _listing(raw: dict, where: str) -> Listing:
    allowed = {"quarterly", "monthly", "serial_nearest", "serial_months"}
    if not raw or set(raw) - allowed or any(not isinstance(v, int) or v < 1 for v in raw.values()):
        raise FuturesSeedError(f"{where}: listing needs positive whole numbers of {sorted(allowed)}")
    if "monthly" in raw and len(raw) > 1:
        raise FuturesSeedError(f"{where}: a monthly listing can't also have quarterly or serial months")
    if "monthly" not in raw and "quarterly" not in raw:
        raise FuturesSeedError(f"{where}: listing needs quarterly or monthly")
    if "serial_nearest" in raw and "serial_months" in raw:
        raise FuturesSeedError(f"{where}: serial_nearest or serial_months, not both")
    return Listing(**raw)


def parse(body: bytes) -> FuturesSeed:
    try:
        raw = tomllib.loads(body.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise FuturesSeedError(f"futures seed: not valid TOML: {e}") from None
    read_on = _date(raw.get("read_on"), "futures seed read_on")
    products: list[ProductSeed] = []
    seen: dict[str, str] = {}
    for i, item in enumerate(raw.get("products", [])):
        root = str(item.get("root", ""))
        where = f"futures seed product {root or i + 1}"
        code = str(item.get("cme_code", ""))
        if not ROOT.match(root):
            raise FuturesSeedError(f"{where}: bad root")
        if not CODE.match(code):
            raise FuturesSeedError(f"{where}: bad cme_code")
        for key in (root, f"cme:{code}"):
            if key in seen:
                raise FuturesSeedError(f"{where}: {key} is also {seen[key]}'s")
            seen[key] = root
        kind = item.get("kind")
        if kind not in KINDS:
            raise FuturesSeedError(f"{where}: kind must be one of {sorted(KINDS)}")
        cals = {}
        for k in ("trade_calendars", "settle_calendars"):
            v = item.get(k)
            if not v or not all(isinstance(c, str) and CALENDAR.match(c) for c in v):
                raise FuturesSeedError(f"{where}: {k} must list calendar-svc names")
            cals[k] = tuple(v)
        rules = item.get("rules") or {}
        unknown = set(rules) - set(RULE_FIELDS)
        if unknown:
            raise FuturesSeedError(f"{where}: unknown rule fields {sorted(unknown)}")
        missing = REQUIRED_RULES[kind] - set(rules)
        if missing:
            raise FuturesSeedError(f"{where}: a {kind} product needs rules for {sorted(missing)}")
        for f, name in rules.items():
            try:
                check_rule(f, str(name))
            except RuleError as e:
                raise FuturesSeedError(f"{where}: {e}") from None
        sources = item.get("rule_sources") or {}
        if set(sources) != set(rules) or not all(isinstance(v, str) and v.strip() for v in sources.values()):
            raise FuturesSeedError(f"{where}: rule_sources must say where every rule comes from")
        generics = item.get("generics")
        if not isinstance(generics, int) or not 1 <= generics <= 24:
            raise FuturesSeedError(f"{where}: generics must be 1 to 24")
        history = _date(item["history_from"], where) if item.get("history_from") else None
        product = Product(root=root, cme_code=code, kind=kind, trade_calendars=cals["trade_calendars"],
                          settle_calendars=cals["settle_calendars"], rules={k: str(v) for k, v in rules.items()},
                          listing=_listing(item.get("listing") or {}, where), history_from=history,
                          generics=generics)
        if product.monthly and generics > product.listing.monthly:
            raise FuturesSeedError(f"{where}: more generics than listed months")
        if not product.monthly and generics > product.listing.quarterly:
            raise FuturesSeedError(f"{where}: more generics than listed quarterly contracts")
        products.append(ProductSeed(
            product=product, name=_text(item, "name", where), clearing_code=_text(item, "clearing_code", where),
            currency=_text(item, "currency", where), root_confirmed=bool(item.get("root_confirmed", False)),
            root_source=_text(item, "root_source", where), history_source=_text(item, "history_source", where),
            rule_sources={k: str(v) for k, v in sources.items()}, specs=_specs(item.get("specs"), where)))
    if not products:
        raise FuturesSeedError("futures seed: no products")
    return FuturesSeed(hashlib.sha256(body).hexdigest(), read_on, tuple(products))


def load(path: Path = PATH) -> FuturesSeed:
    return parse(path.read_bytes())
