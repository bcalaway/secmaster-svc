"""TIPS reference CPI and index ratios (mkt-data's docs/phase-3.md, step 3e): pure functions.

Treasury's rule (31 CFR 356, Appendix B):

- The **reference CPI** for a day in month M is interpolated between the CPI-U
  (all items, not seasonally adjusted: BLS series CUUR0000SA0) of month M-3
  and month M-2: Ref CPI = CPI(M-3) + (day - 1) / (days in M) x (CPI(M-2) - CPI(M-3)).
  So the 1st of the month has CPI(M-3) exactly.
- The **index ratio** of a TIPS on a day is that day's reference CPI over the
  security's own reference CPI on its dated date (its base, published by
  TreasuryDirect).
- A month BLS doesn't publish (October 2025, during the shutdown) is replaced
  by the CFR's fallback: CPI(M-1) x (CPI(M-1) / CPI(M-13)) ^ (1/12). Only a
  gap of at most MAX_FALLBACK_MONTHS is a month BLS didn't publish; a longer
  one is history mkt-data hasn't loaded yet, left empty, so the days that
  need it have no reference CPI (and the TIPS check counts them as not
  computable) rather than a chain of fallbacks standing in for real CPIs.

Rounding: TreasuryDirect publishes reference CPIs and index ratios to five
decimals ("324.05886", "0.99991"), so both are rounded half-up to five; the
load checks that against every TIPS auction TreasuryDirect has published.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

FIVE = Decimal("0.00001")
MAX_FALLBACK_MONTHS = 1


def _month(d: date, delta: int = 0) -> date:
    k = d.year * 12 + d.month - 1 + delta
    return date(k // 12, k % 12 + 1, 1)


def round5(x: Decimal) -> Decimal:
    return x.quantize(FIVE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class MonthCpi:
    value: Decimal
    method: str  # published | fallback


def fill_months(published: dict[date, Decimal]) -> dict[date, MonthCpi]:
    """Every month from the first published one to the last, with the fallback for a month BLS didn't publish.

    A gap longer than MAX_FALLBACK_MONTHS is history not loaded yet: its months are left out.
    """
    if not published:
        return {}
    out: dict[date, MonthCpi] = {}
    m, last = min(published), max(published)
    while m <= last:
        if m in published:
            out[m] = MonthCpi(published[m], "published")
        elif _gap(published, m) > MAX_FALLBACK_MONTHS:
            pass  # not loaded: no CPI, so no reference CPI for the days that need it
        else:
            prev, year_before = out.get(_month(m, -1)), out.get(_month(m, -13))
            if prev is None or year_before is None:
                raise ValueError(f"CPI for {m:%Y-%m} is missing and can't be filled: no CPI a year before it")
            with localcontext() as ctx:
                ctx.prec = 34
                value = prev.value * (prev.value / year_before.value) ** (Decimal(1) / 12)
            out[m] = MonthCpi(round(value, 3), "fallback")  # BLS prints three decimals
        m = _month(m, 1)
    return out


def _gap(published: dict[date, Decimal], m: date) -> int:
    """How many months in a row, around m, have no published CPI."""
    n, k = 1, _month(m, -1)
    while k not in published and k >= min(published):
        n, k = n + 1, _month(k, -1)
    k = _month(m, 1)
    while k not in published and k <= max(published):
        n, k = n + 1, _month(k, 1)
    return n


@dataclass(frozen=True)
class RefCpi:
    day: date
    value: Decimal  # rounded to five decimals, as Treasury publishes it
    method: str  # published (both months published) | fallback (one used the CFR fallback)


def ref_cpi(day: date, months: dict[date, MonthCpi]) -> RefCpi | None:
    """The reference CPI for one day, or None if its months aren't known."""
    lo, hi = months.get(_month(day, -3)), months.get(_month(day, -2))
    if lo is None or hi is None:
        return None
    n = monthrange(day.year, day.month)[1]
    value = lo.value + Decimal(day.day - 1) / Decimal(n) * (hi.value - lo.value)
    method = "fallback" if "fallback" in (lo.method, hi.method) else "published"
    return RefCpi(day, round5(value), method)


def ref_cpi_series(months: dict[date, MonthCpi]) -> list[RefCpi]:
    """Every day whose reference CPI the known months give: from 3 months after the first to the end of
    2 months after the last."""
    if not months:
        return []
    start = _month(min(months), 3)
    end = _month(max(months), 3) - timedelta(days=1)
    out, d = [], start
    while d <= end:
        r = ref_cpi(d, months)
        if r is not None:
            out.append(r)
        d += timedelta(days=1)
    return out


def index_ratio(ref: Decimal, base: Decimal) -> Decimal:
    return round5(ref / base)
