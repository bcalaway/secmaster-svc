"""Futures contracts from product rules (mkt-data's docs/phase-4.md, step 2a).

CME lists its futures on a published cycle, with every date set by its
rulebook, so a contract isn't read from a feed: it's generated from the
product's rules (seeds/futures.toml) and the business days of the calendars
those rules count in (calendar-svc's closed days). Pure functions: no I/O.
app/futures_load.py fetches the calendars and stores the result.

**Date rules.** Each date field names a rule (`last_bd_minus:7`), and the rule
recorded with the date says exactly how it was derived:

- `last_bd`, `first_bd`: the contract month's last or first business day.
- `last_bd_minus:N`, `first_bd_minus:N`: N business days before it.
- `last_bd_plus:N`: N business days after the month's last business day.
- `third_wed_minus:N`: N business days before the month's 3rd Wednesday.
- `monday_before_third_wed`: the Monday before the 3rd Wednesday, or the next
  business day if that Monday isn't one (CME's T-bill futures).
- `third_wed_next_good`: the 3rd Wednesday, or the next day that is a business
  day in every settlement calendar (FX delivery).
- `ltd_plus:N`: N business days after the last trading day.
- `ref_end_minus:N`: N business days before the reference period's end
  (CME's Three-Month SOFR: the business day before the 3rd Wednesday of the
  month its reference quarter ends in).
- Reference periods: `month` (the calendar month) and `imm_quarter` (the
  contract month's 3rd Wednesday to the 3rd Wednesday three months later: CME
  names a Three-Month SOFR contract by the month its reference quarter starts,
  so SR3U6 runs from Sep 16 to Dec 16, 2026 and still trades in October).
  Both ends are stored with the end excluded, as CME's rules state them.

A rule counts business days in the product's trade calendars (or settlement
calendars, for delivery and final settlement): a day is a business day when
it's a weekday and none of those calendars is closed. Early closes are
business days.

**Listing.** Which contracts CME lists on a day follows the product's cycle,
as CME's spec page states it (`quarterly`: that many consecutive March-cycle
contracts; `monthly`: that many consecutive months; `serial_nearest`: the
nearest that many non-quarterly months; `serial_months`: the non-quarterly
months within that many months). A contract's first trading day is derived
only while it's listed today: the first business day it has been listed
since, under today's cycle. Earlier cycles aren't recorded, so an expired
contract's first trading day is unknown (None), and serial months are
generated only while listed: their history isn't known.

**Generics** (`TY1`, `TY2`): the nth contract in order that hasn't rolled.
A Treasury contract rolls on its first intention day, when positions roll
(Bill, 2026-10-07); a STIR or FX contract after its last trading day. For a
product with both quarterly and serial months the generics follow the
quarterly contracts, the only ones with a full history.
"""

import calendar as _cal
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, timedelta

MONTH_CODES = "FGHJKMNQUVXZ"
QUARTERLY = frozenset({3, 6, 9, 12})
KINDS = frozenset({"treasury", "stir", "fx"})
TYPES = {"treasury": "fut_treasury", "stir": "fut_stir", "fx": "fut_fx"}

# Date fields a contract can carry, in display order.
DATE_FIELDS = (
    "first_trade_date", "last_trade_date", "first_intention_date", "first_notice_date",
    "first_delivery_date", "last_delivery_date", "reference_start", "reference_end",
    "final_settlement_date", "settlement_date",
)
# Rule fields the seed may set, and which calendars they count in.
TRADE_RULES = ("last_trade_date", "first_intention_date", "first_notice_date")
SETTLE_RULES = ("first_delivery_date", "last_delivery_date", "final_settlement_date", "settlement_date")
RULE_FIELDS = TRADE_RULES + SETTLE_RULES + ("reference",)


class RuleError(ValueError):
    pass


class CalendarError(ValueError):
    pass


@dataclass(frozen=True)
class Listing:
    quarterly: int = 0
    monthly: int = 0
    serial_nearest: int = 0
    serial_months: int = 0

    @property
    def has_serials(self) -> bool:
        return bool(self.serial_nearest or self.serial_months)


@dataclass(frozen=True)
class Product:
    root: str  # Bloomberg root, the product's short name: TY
    cme_code: str  # CME Globex code: ZN
    kind: str  # treasury | stir | fx
    trade_calendars: tuple[str, ...]  # CME-IR; CME-FX and FED for FX
    settle_calendars: tuple[str, ...]  # delivery country and FED for FX
    rules: dict  # field -> rule name
    listing: Listing
    history_from: date | None  # None: only contracts listed today
    generics: int

    @property
    def monthly(self) -> bool:
        return self.listing.monthly > 0

    @property
    def calendars(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.trade_calendars + self.settle_calendars))


@dataclass
class Calendars:
    """Closed weekdays per calendar, and the years each covers."""

    closed: dict[str, frozenset[date]]
    years: dict[str, tuple[int, int]]

    def check(self, names, start: date, end: date) -> None:
        for n in names:
            if n not in self.years:
                raise CalendarError(f"calendar {n} isn't loaded")
            first, last = self.years[n]
            if start.year < first or end.year > last:
                raise CalendarError(f"calendar {n} covers {first}-{last}, not {start.year}-{end.year}")

    def business(self, day: date, names) -> bool:
        return day.weekday() < 5 and not any(day in self.closed[n] for n in names)


@dataclass(frozen=True)
class Contract:
    product: Product
    month: date  # first of the contract month

    @property
    def code(self) -> str:
        return MONTH_CODES[self.month.month - 1]

    @property
    def short_name(self) -> str:
        """Bloomberg's ticker with a two-digit year, so a name never changes: TYZ26."""
        return f"{self.product.root}{self.code}{self.month.year % 100:02d}"

    @property
    def cme_symbol(self) -> str:
        """CME's code while listed: ZNZ6. It comes round again every ten years."""
        return f"{self.product.cme_code}{self.code}{self.month.year % 10}"

    @property
    def quarterly(self) -> bool:
        return self.month.month in QUARTERLY


@dataclass
class Dated:
    contract: Contract
    dates: dict[str, date | None]
    rules: dict[str, str]  # field -> how it was derived
    status: str = ""
    listed_today: bool = False

    @property
    def last_trade(self) -> date:
        return self.dates["last_trade_date"]

    @property
    def roll_end(self) -> date:
        """The last day it's the front contract."""
        if self.contract.product.kind == "treasury":
            return self.dates["first_intention_date"] - timedelta(days=1)
        return self.last_trade


@dataclass(frozen=True)
class Interval:
    alias: str  # TY1
    contract: Contract
    valid_from: date
    valid_to: date | None  # inclusive


@dataclass
class Generated:
    contracts: list[Dated] = field(default_factory=list)
    generics: list[Interval] = field(default_factory=list)


# --- business-day arithmetic ------------------------------------------------

def _month_end(month: date) -> date:
    return month.replace(day=_cal.monthrange(month.year, month.month)[1])


def _add_months(month: date, n: int) -> date:
    y, m = divmod(month.year * 12 + month.month - 1 + n, 12)
    return date(y, m + 1, 1)


def third_wednesday(month: date) -> date:
    first = month.replace(day=1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 14)


def _step(cals: Calendars, names, day: date, n: int) -> date:
    """n business days after (n > 0) or before (n < 0) day; day itself isn't counted."""
    step = 1 if n > 0 else -1
    left = abs(n)
    while left:
        day += timedelta(days=step)
        if cals.business(day, names):
            left -= 1
    return day


def _on_or_after(cals: Calendars, names, day: date) -> date:
    while not cals.business(day, names):
        day += timedelta(days=1)
    return day


def _on_or_before(cals: Calendars, names, day: date) -> date:
    while not cals.business(day, names):
        day -= timedelta(days=1)
    return day


def _rule(name: str) -> tuple[str, int]:
    base, _, arg = name.partition(":")
    if arg and not arg.isdigit():
        raise RuleError(f"rule {name!r}: the argument must be a whole number")
    return base, int(arg or 0)


BASES = {"last_bd", "first_bd", "last_bd_minus", "first_bd_minus", "last_bd_plus", "third_wed_minus",
         "monday_before_third_wed", "third_wed_next_good", "ltd_plus", "ref_end_minus"}
NEEDS_ARG = {"last_bd_minus", "first_bd_minus", "last_bd_plus", "third_wed_minus", "ltd_plus", "ref_end_minus"}
REFERENCES = {"month", "imm_quarter"}


def check_rule(field_: str, name: str) -> None:
    if field_ == "reference":
        if name not in REFERENCES:
            raise RuleError(f"reference {name!r}: expected one of {sorted(REFERENCES)}")
        return
    base, arg = _rule(name)
    if base not in BASES:
        raise RuleError(f"{field_}: unknown rule {name!r}")
    if (base in NEEDS_ARG) != (arg > 0):
        raise RuleError(f"{field_}: rule {name!r} " + ("needs" if base in NEEDS_ARG else "takes no") + " argument")
    if base == "ltd_plus" and field_ == "last_trade_date":
        raise RuleError("last_trade_date can't count from itself")


def _apply_rule(name: str, month: date, cals: Calendars, names, ltd: date | None,
                ref_end: date | None = None) -> date:
    base, n = _rule(name)
    if base == "ref_end_minus":
        if ref_end is None:
            raise RuleError(f"rule {name!r} needs a reference period")
        return _step(cals, names, ref_end, -n)
    if base == "last_bd":
        return _on_or_before(cals, names, _month_end(month))
    if base == "first_bd":
        return _on_or_after(cals, names, month)
    if base == "last_bd_minus":
        return _step(cals, names, _on_or_before(cals, names, _month_end(month)), -n)
    if base == "first_bd_minus":
        return _step(cals, names, _on_or_after(cals, names, month), -n)
    if base == "last_bd_plus":
        return _step(cals, names, _on_or_before(cals, names, _month_end(month)), n)
    if base == "third_wed_minus":
        return _step(cals, names, third_wednesday(month), -n)
    if base == "monday_before_third_wed":
        return _on_or_after(cals, names, third_wednesday(month) - timedelta(days=2))
    if base == "third_wed_next_good":
        return _on_or_after(cals, names, third_wednesday(month))
    if base == "ltd_plus":
        return _step(cals, names, ltd, n)
    raise RuleError(f"unknown rule {name!r}")


def _describe(name: str, names) -> str:
    return f"{name} ({' + '.join(names)} business days)"


def dates_for(c: Contract, cals: Calendars) -> Dated:
    """Every rule-derived date of one contract (not the first trading day: that needs the listing)."""
    p = c.product
    dates: dict[str, date | None] = dict.fromkeys(DATE_FIELDS)
    rules: dict[str, str] = {}
    ref = p.rules.get("reference")
    if ref == "month":
        dates["reference_start"], dates["reference_end"] = c.month, _add_months(c.month, 1)
        rules["reference_start"] = rules["reference_end"] = "month (calendar month; end excluded)"
    elif ref == "imm_quarter":
        dates["reference_start"] = third_wednesday(c.month)
        dates["reference_end"] = third_wednesday(_add_months(c.month, 3))
        rules["reference_start"] = rules["reference_end"] = (
            "imm_quarter (the contract month's 3rd Wednesday to the 3rd Wednesday three months later; end excluded)")
    ltd_rule = p.rules["last_trade_date"]
    dates["last_trade_date"] = _apply_rule(ltd_rule, c.month, cals, p.trade_calendars, None, dates["reference_end"])
    rules["last_trade_date"] = _describe(ltd_rule, p.trade_calendars)
    for f in TRADE_RULES[1:] + SETTLE_RULES:
        name = p.rules.get(f)
        if not name:
            continue
        names = p.trade_calendars if f in TRADE_RULES else p.settle_calendars
        dates[f] = _apply_rule(name, c.month, cals, names, dates["last_trade_date"], dates["reference_end"])
        rules[f] = _describe(name, names)
    return Dated(c, dates, rules)


# --- listing and generation --------------------------------------------------

def listed_on(p: Product, ordered: list[Dated], ltds: list[date], day: date) -> list[Dated]:
    """The contracts CME lists on a day under the product's current cycle. `ordered` is by month; `ltds` is
    the running maximum of their last trading days, for the search."""
    start = bisect_left(ltds, day)
    alive = [d for d in ordered[start:] if d.last_trade >= day]
    L = p.listing
    if L.monthly:
        return alive[:L.monthly]
    out = [d for d in alive if d.contract.quarterly][:L.quarterly]
    serials = [d for d in alive if not d.contract.quarterly]
    if L.serial_nearest:
        out += serials[:L.serial_nearest]
    if L.serial_months:
        horizon = _add_months(day.replace(day=1), L.serial_months)
        out += [d for d in serials if d.contract.month < horizon]
    return sorted(out, key=lambda d: d.contract.month)


def _first_trades(p: Product, ordered: list[Dated], ltds: list[date], targets: list[Dated], today: date,
                  floor: date, cals: Calendars) -> dict[int, date]:
    """Each target's first trading day: the first business day of the unbroken run of days, ending on the
    last day it was listed (today, or its last trading day), on which the cycle lists it.

    The listed set only changes the day after a last trading day or on the first of a month (the
    `serial_months` window), so membership is checked once per stretch between those days."""
    points = sorted({d.last_trade + timedelta(days=1) for d in ordered} | {m.contract.month for m in ordered}
                    | {floor})
    points = [x for x in points if floor <= x <= today]
    members: dict[date, set[int]] = {}

    def listed_from(i: int) -> set[int]:
        if points[i] not in members:
            members[points[i]] = {id(x) for x in listed_on(p, ordered, ltds, points[i])}
        return members[points[i]]

    out: dict[int, date] = {}
    for d in targets:
        last_day = min(today, d.last_trade)
        i = bisect_left(points, last_day + timedelta(days=1)) - 1  # the stretch holding last_day
        if i < 0 or id(d) not in listed_from(i):
            continue
        while i > 0 and id(d) in listed_from(i - 1):
            i -= 1
        out[id(d)] = _on_or_after(cals, p.trade_calendars, points[i])
    return out


def _months(p: Product, start: date, end: date) -> list[date]:
    out, m = [], start.replace(day=1)
    while m <= end:
        out.append(m)
        m = _add_months(m, 1)
    return out


def generate(p: Product, cals: Calendars, today: date, horizon_years: int = 15) -> Generated:
    """Every contract from the product's history start to the furthest listed today, with dates, status and
    the generics' intervals to today."""
    # Candidates reach back far enough to replay the listing cycle to today's listings, and to the history.
    window_start = _add_months(today.replace(day=1), -horizon_years * 12)
    first_month = min(window_start, p.history_from.replace(day=1)) if p.history_from else window_start
    last_month = _add_months(today.replace(day=1), horizon_years * 12)
    cals.check(p.calendars, first_month, _add_months(last_month, 2))
    candidates = [dates_for(Contract(p, m), cals) for m in _months(p, first_month, last_month)]
    ordered = sorted(candidates, key=lambda d: d.contract.month)
    ltds, top = [], date.min
    for d in ordered:
        top = max(top, d.last_trade)
        ltds.append(top)
    listed = listed_on(p, ordered, ltds, today)
    if not listed:
        raise RuleError(f"{p.root}: nothing listed on {today}")
    listed_ids = {id(d) for d in listed}
    furthest = max(d.contract.month for d in listed)

    keep: list[Dated] = []
    for d in ordered:
        if d.contract.month > furthest:
            break
        is_listed = id(d) in listed_ids
        d.listed_today = is_listed
        d.status = _status(d, today)
        if p.history_from is None and not is_listed and d.status != "delivery":
            continue
        if p.history_from is not None and d.last_trade < p.history_from:
            continue
        if not is_listed and not p.monthly and not d.contract.quarterly:
            continue  # a serial month's history isn't known
        if not is_listed and d.last_trade >= today:
            continue  # inside the window but not listed under the cycle: not listed yet
        keep.append(d)
    # First trading days: for what's listed or in delivery today, replaying today's cycle.
    recent = [d for d in keep if d.listed_today or d.status == "delivery"]
    firsts = _first_trades(p, ordered, ltds, recent, today, window_start, cals)
    for d in keep:
        if id(d) in firsts:
            d.dates["first_trade_date"] = firsts[id(d)]
            d.rules["first_trade_date"] = ("derived: listed since, under the listing cycle on CME's spec page "
                                           "(read 2026-10-08), assumed in force since listing")
        else:
            d.rules["first_trade_date"] = "unknown: listing cycles before 2026-10-08 aren't recorded"
    return Generated(keep, generic_intervals(p, keep, today))


def _status(d: Dated, today: date) -> str:
    if d.contract.product.kind == "treasury":
        if today > d.dates["last_delivery_date"]:
            return "expired"
        if today >= d.dates["first_intention_date"]:
            return "delivery"
        return "listed"
    return "expired" if today > d.last_trade else "listed"


def generic_intervals(p: Product, contracts: list[Dated], today: date) -> list[Interval]:
    """`TY1`..`TYn` with validity, from the product's history start (or the earliest contract) to today."""
    seq = sorted((d for d in contracts if p.monthly or d.contract.quarterly), key=lambda d: d.contract.month)
    if not seq:
        return []
    start = p.history_from or today
    out: list[Interval] = []
    prev_end: date | None = None
    for i, front in enumerate(seq):
        frm = start if prev_end is None else max(start, prev_end + timedelta(days=1))
        to = front.roll_end
        prev_end = to
        if to < frm:
            continue
        if frm > today:
            break
        for n in range(1, p.generics + 1):
            if i + n - 1 >= len(seq):
                break
            out.append(Interval(f"{p.root}{n}", seq[i + n - 1].contract, frm, to))
    return out
