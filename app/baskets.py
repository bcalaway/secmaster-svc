"""Treasury futures' deliverable baskets and conversion factors (mkt-data's docs/phase-4.md, step 3).

Which securities a short may deliver, and the conversion factor each is invoiced at, are fixed by
CME's rules from the security's terms, which phase 3 holds for every Treasury since 1980. Pure
functions: no I/O. app/futures_load.py stores the result as futures_deliverable rows.

**Eligibility** (each product's `basket` in seeds/futures.toml, from its CBOT chapter): a
fixed-principal note or bond with a fixed semi-annual coupon (no bills, TIPS, FRNs or STRIPS), within
the product's remaining-term window measured from the first day of the contract month (to the first
call date for a callable bond; for the 2-, 3- and 5-year notes, rounded down to whole months first, as
their chapters say), and within its original-term limit. A security joins a
contract's basket from its issue date, so one auctioned after the contract was listed is in it from
then (`valid_from`); one issued after the last delivery day isn't in it. CME may exclude a new issue
by notice: not modelled.

**Conversion factor** (CME's published method): the price per 1 of par at which the security, with
its remaining term rounded down to whole months (2-, 3- and 5-year) or to quarters (the rest), yields
the notional 6%, rounded to four decimals. With n whole years and z months:

    v = z if z < 7 else z - 6
    a = 1 / 1.03 ** (v / 6)
    b = (coupon / 2) * (6 - v) / 6
    c = 1 / 1.03 ** (2n) if z < 7 else 1 / 1.03 ** (2n + 1)
    d = (coupon / 0.06) * (1 - c)
    factor = a * (coupon / 2 + c + d) - b

The notional coupon was 8% before the March 2000 contracts; baskets are computed only for contracts
on the 6% rules (the earlier ones aren't generated with baskets yet).
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Context, Decimal

NOTIONAL = Decimal("0.06")
SIX_PERCENT_FROM = date(2000, 3, 1)
CTX = Context(prec=40)
FOUR = Decimal("0.0001")
ROUNDING = ("month", "quarter")


class BasketError(ValueError):
    pass


@dataclass(frozen=True)
class BasketRule:
    """A product's deliverable grade, in months (CBOT's chapters state years and months)."""

    remaining_min: int  # inclusive
    remaining_max: int | None = None
    max_inclusive: bool = True  # "<= 2 years" (True) or "< 8 years" (False)
    original_max: int | None = None  # inclusive; None: no limit
    original_max_from: date | None = None  # the original-term limit only for securities issued from this date
    rounding: str = "quarter"  # the conversion factor's remaining term: month or quarter
    to_first_call: bool = False  # a callable bond's term runs to its first call

    @property
    def text(self) -> str:
        def ym(m: int) -> str:
            y, mm = divmod(m, 12)
            return f"{y}y{mm}m" if mm else f"{y}y"
        hi = f" and {'<=' if self.max_inclusive else '<'} {ym(self.remaining_max)}" if self.remaining_max else ""
        orig = f"; original term <= {ym(self.original_max)}" if self.original_max else ""
        if self.original_max and self.original_max_from:
            orig += f" if issued from {self.original_max_from}"
        return f"remaining >= {ym(self.remaining_min)}{hi}{orig}; factor rounded to {self.rounding}s"


@dataclass(frozen=True)
class Security:
    """The terms a basket needs (from security_terms)."""

    sec_id: int
    cusip: str
    security_type: str  # note, bond, bill, tips, frn
    coupon_rate: Decimal | None  # decimal: 0.0425
    coupon_frequency: int
    issue_date: date | None  # the original issue
    maturity_date: date
    call_date: date | None = None


@dataclass(frozen=True)
class Deliverable:
    sec_id: int
    cusip: str
    conversion_factor: Decimal
    valid_from: date  # the later of its issue date and the day the contract was first generated
    remaining_months: int  # rounded, as the factor uses it


def parse_rule(raw: dict, where: str) -> BasketRule:
    allowed = {"remaining_min_months", "remaining_max_months", "max_inclusive", "original_max_months",
               "rounding", "to_first_call", "source", "original_max_from"}
    if not isinstance(raw, dict) or set(raw) - allowed or "remaining_min_months" not in raw:
        raise BasketError(f"{where}: basket needs remaining_min_months, and only {sorted(allowed)}")
    ints = {k: raw.get(k) for k in ("remaining_min_months", "remaining_max_months", "original_max_months")}
    for k, v in ints.items():
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 < v <= 600):
            raise BasketError(f"{where}: {k} must be a whole number of months")
    if ints["remaining_max_months"] is not None and ints["remaining_max_months"] <= ints["remaining_min_months"]:
        raise BasketError(f"{where}: remaining_max_months must be above remaining_min_months")
    rounding = raw.get("rounding", "quarter")
    if rounding not in ROUNDING:
        raise BasketError(f"{where}: rounding must be one of {ROUNDING}")
    since = raw.get("original_max_from")
    if since is not None and (not isinstance(since, date) or ints["original_max_months"] is None):
        raise BasketError(f"{where}: original_max_from must be a date, with original_max_months")
    return BasketRule(remaining_min=ints["remaining_min_months"], remaining_max=ints["remaining_max_months"],
                      max_inclusive=bool(raw.get("max_inclusive", True)), original_max=ints["original_max_months"],
                      original_max_from=since, rounding=rounding, to_first_call=bool(raw.get("to_first_call", False)))


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(d.day, last))


def whole_months(start: date, end: date) -> int:
    """Whole months from start to end, rounded down."""
    n = (end.year - start.year) * 12 + end.month - start.month
    return n - 1 if add_months(start, n) > end else n


def conversion_factor(coupon: Decimal, months: int) -> Decimal:
    """CME's conversion factor for a coupon (decimal) and a remaining term already rounded, in months."""
    n, z = divmod(months, 12)
    v = Decimal(z if z < 7 else z - 6)
    r = Decimal("1.03")
    a = CTX.divide(1, CTX.power(r, CTX.divide(v, 6)))
    b = coupon / 2 * (6 - v) / 6
    c = CTX.divide(1, CTX.power(r, 2 * n if z < 7 else 2 * n + 1))
    d = CTX.divide(coupon, NOTIONAL) * (1 - c)
    factor = CTX.subtract(CTX.multiply(a, coupon / 2 + c + d), b)
    return factor.quantize(FOUR, rounding=ROUND_HALF_UP)


def _rounded(months: int, rule: BasketRule) -> int:
    return months - months % 3 if rule.rounding == "quarter" else months


def eligible(rule: BasketRule, sec: Security, contract_month: date, last_delivery: date) -> int | None:
    """The rounded remaining term in months if the security is deliverable into the contract, else None."""
    if sec.security_type not in ("note", "bond") or sec.coupon_frequency != 2 or sec.coupon_rate is None:
        return None
    if sec.issue_date is None or sec.issue_date > last_delivery:
        return None
    end = sec.call_date if (rule.to_first_call and sec.call_date) else sec.maturity_date
    if rule.rounding == "month":
        # The 2-, 3- and 5-year chapters: the remaining term "rounded down to the nearest one-month increment".
        m = whole_months(contract_month, end)
        if m < rule.remaining_min or (rule.remaining_max is not None and (
                m > rule.remaining_max or (m == rule.remaining_max and not rule.max_inclusive))):
            return None
    else:
        if end < add_months(contract_month, rule.remaining_min):
            return None
        if rule.remaining_max is not None:
            top = add_months(contract_month, rule.remaining_max)
            if end > top or (end == top and not rule.max_inclusive):
                return None
    if (rule.original_max is not None and sec.maturity_date > add_months(sec.issue_date, rule.original_max)
            and (rule.original_max_from is None or sec.issue_date >= rule.original_max_from)):
        return None
    return _rounded(whole_months(contract_month, end), rule)


def basket(rule: BasketRule, securities: list[Security], contract_month: date, last_delivery: date) -> list[Deliverable]:
    """Every deliverable security with its conversion factor, by maturity."""
    if contract_month < SIX_PERCENT_FROM:
        raise BasketError(f"{contract_month:%Y-%m}: before the 6% notional coupon (March 2000); not modelled")
    out = []
    for sec in sorted(securities, key=lambda x: (x.maturity_date, x.cusip)):
        months = eligible(rule, sec, contract_month, last_delivery)
        if months is None:
            continue
        out.append(Deliverable(sec.sec_id, sec.cusip, conversion_factor(sec.coupon_rate, months),
                               sec.issue_date, months))
    return out
