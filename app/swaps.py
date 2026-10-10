"""OIS par swaps from seeds/swaps.toml: instruments and their conventions (mkt-data's docs/phase-4.md, "Swap curves").

The seed lists the ISDA CDS Standard Model's RFR curves (one per currency, published by S&P Global Market
Intelligence, captured by mkt-data as SPGMI-RFR-<CCY>), each with its conventions and tenors. Applying it:

- builds one instrument per curve and tenor (type `swap_ois`, short name `USD-SOFR-OIS-10Y`, curve `ISDA-RFR-USD`,
  tenor `P10Y`, identifier SPGMI-RFR-USD=10Y) and applies them through the ordinary seed machinery (app/seed.py), so
  sec_ids, names and identifiers behave as for the CMTs and fixings;
- records each instrument's conventions in `swap_terms`: a changed convention supersedes the current row.

Runs with the other seeds (app/seed.py's apply_all: every start and POST /jobs/seed). A bad file changes nothing.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import seed
from app.models import InstrumentName, SwapTerms

PATH = seed.SEEDS / "swaps.toml"
TYPE = "swap_ois"
SOURCE_TENOR = re.compile(r"^(\d{1,2})([MY])$")
DAY_COUNTS = {"ACT/360", "ACT/365"}
CONVENTIONS = {"modified_following"}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CURVE_KEYS = ("curve", "currency", "country", "index", "source", "calendar", "timezone", "money_market_day_count",
              "fixed_day_count", "floating_day_count", "spot_lag_days", "spot_calendar", "adjust_calendar", "tenors")
TERM_KEYS = ("cite", "fixed_frequency", "floating_frequency", "business_day_convention", "zero_coupon_through",
             "model_instrument_type", "snap_time", "publication_time", "publication_deadline")
# The swap_terms columns compared to decide whether a row changed.
COLUMNS = ("curve", "floating_index", "money_market_day_count", "fixed_day_count", "floating_day_count",
           "fixed_frequency", "floating_frequency", "coupon", "zero_coupon_through", "spot_lag_days", "spot_calendar",
           "adjust_calendar", "business_day_convention", "model_instrument_type", "snap_time", "publication_time",
           "publication_deadline", "timezone", "source", "source_key", "cite")


@dataclass(frozen=True)
class Swap:
    instrument: seed.SeedInstrument
    terms: dict  # COLUMNS -> value


def months(duration: str) -> int:
    """P1M -> 1, P10Y -> 120 (the seed's frequencies and tenors only use months and years)."""
    m = re.fullmatch(r"P(\d+)([MY])", duration)
    if not m:
        raise seed.SeedError(f"{duration!r}: expected PnM or PnY")
    return int(m[1]) * (12 if m[2] == "Y" else 1)


def parse(body: bytes, name: str = "swaps") -> tuple[seed.Seed, list[Swap]]:
    try:
        raw = tomllib.loads(body.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise seed.SeedError(f"{name}: not valid TOML: {e}") from None
    defaults = raw.get("defaults", {})
    swaps: list[Swap] = []
    curves_seen: set[str] = set()
    for c in raw.get("curves", []):
        cur = {k: c.get(k, defaults.get(k)) for k in CURVE_KEYS + TERM_KEYS}
        where = f"{name} curve {cur['curve'] or '?'}"
        missing = [k for k in CURVE_KEYS + TERM_KEYS
                   if cur[k] is None or (cur[k] == "" and k not in ("spot_calendar", "adjust_calendar"))]
        if missing:
            raise seed.SeedError(f"{where}: missing {', '.join(missing)}")
        if cur["curve"] in curves_seen:
            raise seed.SeedError(f"{where}: listed twice")
        curves_seen.add(cur["curve"])
        for k in ("money_market_day_count", "fixed_day_count", "floating_day_count"):
            if cur[k] not in DAY_COUNTS:
                raise seed.SeedError(f"{where}: {k} {cur[k]!r} isn't one of {sorted(DAY_COUNTS)}")
        if cur["business_day_convention"] not in CONVENTIONS:
            raise seed.SeedError(f"{where}: business_day_convention {cur['business_day_convention']!r}")
        for k in ("snap_time", "publication_time", "publication_deadline"):
            if not HHMM.match(str(cur[k])):
                raise seed.SeedError(f"{where}: {k} {cur[k]!r} isn't HH:MM")
        if not isinstance(cur["spot_lag_days"], int) or not 0 <= cur["spot_lag_days"] <= 5:
            raise seed.SeedError(f"{where}: spot_lag_days {cur['spot_lag_days']!r}")
        zero_through = months(cur["zero_coupon_through"])
        months(cur["fixed_frequency"]), months(cur["floating_frequency"])
        tenors = list(cur["tenors"])
        if len(set(tenors)) != len(tenors) or not tenors:
            raise seed.SeedError(f"{where}: tenors must be listed once each")
        for key in tenors:
            m = SOURCE_TENOR.match(key)
            if not m:
                raise seed.SeedError(f"{where}: tenor {key!r} isn't like 1M or 10Y")
            tenor = f"P{m[1]}{m[2]}"
            ccy, index = cur["currency"], cur["index"]
            label = {"M": "month", "Y": "year"}[m[2]]
            inst = seed.SeedInstrument(
                short_name=f"{ccy}-{index}-OIS-{key}",
                aliases=(),
                attrs={"type": TYPE, "currency": ccy, "country": cur["country"], "curve": cur["curve"],
                       "tenor": tenor, "calendar": cur["calendar"],
                       "description": f"{ccy} {index} OIS {m[1]}-{label} par swap rate (ISDA CDS Standard Model curve, "
                                      f"S&P Global Market Intelligence)"},
                identifiers=((cur["source"], key),),
                notes=(),
            )
            terms = {
                "curve": cur["curve"], "floating_index": index,
                "money_market_day_count": cur["money_market_day_count"], "fixed_day_count": cur["fixed_day_count"],
                "floating_day_count": cur["floating_day_count"], "fixed_frequency": cur["fixed_frequency"],
                "floating_frequency": cur["floating_frequency"],
                "coupon": "zero" if months(tenor) <= zero_through else "periodic",
                "zero_coupon_through": cur["zero_coupon_through"], "spot_lag_days": cur["spot_lag_days"],
                "spot_calendar": cur["spot_calendar"] or None, "adjust_calendar": cur["adjust_calendar"] or None,
                "business_day_convention": cur["business_day_convention"],
                "model_instrument_type": cur["model_instrument_type"], "snap_time": cur["snap_time"],
                "publication_time": cur["publication_time"], "publication_deadline": cur["publication_deadline"],
                "timezone": cur["timezone"], "source": cur["source"], "source_key": key, "cite": cur["cite"],
            }
            swaps.append(Swap(inst, terms))
    if not swaps:
        raise seed.SeedError(f"{name}: no curves")
    names = [s.instrument.short_name for s in swaps]
    if len(names) != len(set(names)):
        raise seed.SeedError(f"{name}: two curves make the same short name")
    out = seed.Seed(name=name, sha256=hashlib.sha256(body).hexdigest(),
                    instruments=[s.instrument for s in swaps])
    for inst in out.instruments:  # the generic checks (name and tenor shapes)
        if not seed.NAME.match(inst.short_name) or not seed.TENOR.match(inst.attrs["tenor"]):
            raise seed.SeedError(f"{name}: bad generated instrument {inst.short_name}")
    return out, swaps


def load(path: Path = PATH) -> tuple[seed.Seed, list[Swap]]:
    return parse(path.read_bytes(), path.stem)


def apply(s: Session, parsed: tuple[seed.Seed, list[Swap]], now: datetime | None = None) -> dict:
    """Apply the instruments (app/seed.py, which commits and records the run), then their terms. Commits."""
    now = now or datetime.now(UTC)
    the_seed, swaps = parsed
    summary = seed.apply(s, the_seed, now)
    by_name = seed_sec_ids(s, [w.instrument.short_name for w in swaps])
    current = {t.sec_id: t for t in s.scalars(select(SwapTerms).where(SwapTerms.superseded_at.is_(None)))}
    added = superseded = 0
    for w in swaps:
        sec_id = by_name[w.instrument.short_name]
        row = current.get(sec_id)
        if row is not None and all(getattr(row, k) == w.terms[k] for k in COLUMNS):
            continue
        if row is not None:
            row.superseded_at = now
            superseded += 1
            s.flush()
        s.add(SwapTerms(sec_id=sec_id, recorded_at=now, **w.terms))
        added += 1
    s.commit()
    return summary | {"terms_added": added, "terms_superseded": superseded}


def seed_sec_ids(s: Session, names: list[str]) -> dict[str, int]:
    rows = s.execute(select(InstrumentName.name, InstrumentName.sec_id).where(
        InstrumentName.name.in_(names), InstrumentName.removed_at.is_(None)))
    return dict(rows.all())
