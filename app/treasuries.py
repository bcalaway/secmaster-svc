"""Treasury securities from TreasuryDirect's records (mkt-data's docs/phase-3.md, step 3).

Pure functions, no database: one record (an auction, as mkt-data keeps it in
near-raw, every field a string as TreasuryDirect printed it) becomes a typed
`Auction`; a security's auctions become its `Terms`, each term marked as
published (which record and field it came from) or derived (the rule).
app/load.py stores them.

Conventions: rates and yields are decimals (a 4.25% coupon is 0.0425, a
4.912% high yield 0.04912); prices stay per 100 as published; amounts are
dollars of face. A blank field is None, never zero.

A security is one CUSIP. A reopening is another auction of it, so the terms
come from the original auction when it's loaded, and from a reopening's
`original*` fields when it isn't (yet).
"""

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

SOURCE = "TD-SECURITIES"

# TreasuryDirect's `type` -> (instrument type, security type)
TYPES = {
    "Bill": ("ust_bill", "bill"), "CMB": ("ust_bill", "bill"),
    "Note": ("ust_note", "note"), "Bond": ("ust_bond", "bond"),
    "TIPS": ("ust_tips", "tips"), "FRN": ("ust_frn", "frn"),
}
FREQUENCY = {"Semi-Annual": 2, "Quarterly": 4}
DAY_COUNT = {"bill": "ACT/360", "frn": "ACT/360", "note": "ACT/ACT-ICMA", "bond": "ACT/ACT-ICMA",
             "tips": "ACT/ACT-ICMA"}
FRN_INDEX = "13-week bill high rate"
TERM_PART = re.compile(r"(\d+)-(Year|Month|Week|Day)")
ISO_UNIT = {"Year": "Y", "Month": "M", "Week": "W", "Day": "D"}


class Untypable(ValueError):
    """A record that can't be read as a Treasury security (reported, not loaded)."""


# --- reading TreasuryDirect's strings ---------------------------------------


def _s(f: dict, k: str) -> str | None:
    v = f.get(k)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _date(f: dict, k: str) -> date | None:
    v = _s(f, k)
    if v is None:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        raise Untypable(f"{k}: {v!r} isn't a date") from None


def _num(f: dict, k: str) -> Decimal | None:
    v = _s(f, k)
    if v is None:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise Untypable(f"{k}: {v!r} isn't a number") from None
    if not d.is_finite():
        raise Untypable(f"{k}: {v!r} isn't a finite number")
    return d


def _pct(f: dict, k: str) -> Decimal | None:
    """A rate printed in percent ("4.250000"), as a decimal (0.0425), exactly."""
    d = _num(f, k)
    return None if d is None else (d / 100).normalize()


def _yes(f: dict, k: str) -> bool | None:
    v = _s(f, k)
    return None if v is None else v.lower() == "yes"


def term_iso(term: str | None) -> str | None:
    """TreasuryDirect's term as an ISO 8601 duration: "10-Year" P10Y, "29-Year 10-Month" P29Y10M, "13-Week" P13W."""
    if not term:
        return None
    parts = TERM_PART.findall(term)
    if not parts or TERM_PART.sub("", term).strip():
        raise Untypable(f"term {term!r} isn't N-Year/Month/Week/Day")
    return "P" + "".join(f"{n}{ISO_UNIT[u]}" for n, u in parts if n != "0")


# --- identifiers -------------------------------------------------------------


def _char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c.upper()) - ord("A") + 10
    return {"*": 36, "@": 37, "#": 38}[c]


def cusip_ok(cusip: str) -> bool:
    """CUSIP's own check digit (the ninth character)."""
    if len(cusip) != 9 or not cusip[8].isdigit():
        return False
    total = 0
    for i, c in enumerate(cusip[:8]):
        try:
            v = _char_value(c)
        except KeyError:
            return False
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(cusip[8])


def isin(cusip: str, country: str = "US") -> str:
    """The ISIN for a CUSIP: country + CUSIP + check digit (ISO 6166, Luhn over the letters as numbers)."""
    body = country + cusip
    digits = "".join(str(_char_value(c)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return body + str((10 - total % 10) % 10)


# --- one record -> one auction ----------------------------------------------


@dataclass
class Auction:
    source_key: str  # CUSIP/issue date, as mkt-data keys the record
    cusip: str
    inst_type: str  # ust_bill ...
    security_type: str  # bill ...
    cmb: bool
    reopening: bool
    term: str | None  # the auction program: "10-Year", "13-Week" (a reopened 30-year is still "30-Year")
    security_term: str | None  # this auction's exact term: "29-Year 10-Month"
    original_term: str | None  # the original issue's exact term: "30-Year", "5-Year 2-Month"
    announcement_date: date | None
    auction_date: date | None
    issue_date: date | None
    dated_date: date | None
    maturity_date: date
    original_issue_date: date | None
    original_dated_date: date | None
    coupon_rate: Decimal | None
    frequency: str | None
    first_coupon_date: date | None
    first_period_type: str | None
    callable: bool | None
    call_date: date | None
    called_date: date | None
    strippable: bool | None
    corpus_cusip: str | None
    tips_base_cpi: Decimal | None
    cpi_base_period: str | None
    frn_spread: Decimal | None
    results: dict = field(default_factory=dict)  # typed auction results (see RESULTS)


# Auction results kept as typed columns: name -> (TreasuryDirect field, reader)
RESULTS = {
    "offering_amount": ("offeringAmount", _num),
    "total_tendered": ("totalTendered", _num),
    "total_accepted": ("totalAccepted", _num),
    "bid_to_cover": ("bidToCoverRatio", _num),
    "high_yield": ("highYield", _pct),
    "high_discount_rate": ("highDiscountRate", _pct),
    "high_investment_rate": ("highInvestmentRate", _pct),
    "high_discount_margin": ("highDiscountMargin", _pct),
    "high_price": ("highPrice", _num),
    "price_per_100": ("pricePer100", _num),
    "accrued_interest_per_1000": ("accruedInterestPer1000", _num),
    "adjusted_accrued_interest_per_1000": ("adjustedAccruedInterestPer1000", _num),
    "index_ratio_on_issue_date": ("indexRatioOnIssueDate", _num),
    "ref_cpi_on_issue_date": ("refCpiOnIssueDate", _num),
    "frn_index_rate": ("frnIndexDeterminationRate", _pct),
    "frn_index_determination_date": ("frnIndexDeterminationDate", _date),
    "currently_outstanding": ("currentlyOutstanding", _num),
}


def type_auction(source_key: str, f: dict) -> Auction:
    cusip = _s(f, "cusip")
    if not cusip or len(cusip) != 9:
        raise Untypable(f"cusip {cusip!r} isn't 9 characters")
    td_type = _s(f, "type")
    if td_type not in TYPES:
        raise Untypable(f"type {td_type!r} isn't one of {sorted(TYPES)}")
    inst_type, sec_type = TYPES[td_type]
    maturity = _date(f, "maturityDate")
    if maturity is None:
        raise Untypable("no maturityDate")
    for k in ("term", "securityTerm", "originalSecurityTerm"):
        term_iso(_s(f, k))  # checked here, so a strange term is reported rather than half-loaded
    results = {name: reader(f, k) for name, (k, reader) in RESULTS.items()}
    return Auction(
        source_key=source_key, cusip=cusip, inst_type=inst_type, security_type=sec_type,
        cmb=td_type == "CMB" or bool(_yes(f, "cashManagementBillCMB")),
        reopening=bool(_yes(f, "reopening")),
        term=_s(f, "term"), security_term=_s(f, "securityTerm"), original_term=_s(f, "originalSecurityTerm"),
        announcement_date=_date(f, "announcementDate"), auction_date=_date(f, "auctionDate"),
        issue_date=_date(f, "issueDate"), dated_date=_date(f, "datedDate"), maturity_date=maturity,
        original_issue_date=_date(f, "originalIssueDate"), original_dated_date=_date(f, "originalDatedDate"),
        coupon_rate=_pct(f, "interestRate") if sec_type in ("note", "bond", "tips") else None,
        frequency=_s(f, "interestPaymentFrequency"),
        first_coupon_date=_date(f, "firstInterestPaymentDate"), first_period_type=_s(f, "firstInterestPeriod"),
        callable=_yes(f, "callable"), call_date=_date(f, "callDate"), called_date=_date(f, "calledDate"),
        strippable=_yes(f, "strippable"), corpus_cusip=_s(f, "corpusCusip"),
        tips_base_cpi=_num(f, "refCpiOnDatedDate") if sec_type == "tips" else None,
        cpi_base_period=_s(f, "cpiBaseReferencePeriod"),
        frn_spread=_pct(f, "spread") if sec_type == "frn" else None,
        results=results,
    )


# --- dates -------------------------------------------------------------------


def _eom(d: date) -> bool:
    return d.day == monthrange(d.year, d.month)[1]


def add_months(d: date, months: int, end_of_month: bool) -> date:
    k = d.year * 12 + d.month - 1 + months
    y, m = divmod(k, 12)
    last = monthrange(y, m + 1)[1]
    return date(y, m + 1, last if end_of_month else min(d.day, last))


def regular_schedule(maturity: date, frequency: int, start: date) -> list[date]:
    """Regular coupon dates after `start` up to maturity, generated back from maturity.

    The end-of-month rule: a security maturing on the last day of a month pays
    on the last day of each coupon month (a 2-year note due Feb 29 pays Aug 31).
    """
    step = 12 // frequency
    eom = _eom(maturity)
    out, n = [], 0
    while True:
        d = add_months(maturity, -step * n, eom)
        if d <= start:
            break
        out.append(d)
        n += 1
    return sorted(out)


# --- a security's auctions -> its terms -------------------------------------


@dataclass
class Terms:
    values: dict  # term name -> value (dates, Decimals, ints, strings, bools)
    provenance: dict  # term name -> "published: TD-SECURITIES <key> <field>" or "derived: <rule>"
    checks: list[str]  # anything that doesn't add up, for the metrics and a look by hand


def build_terms(auctions: list[Auction]) -> Terms:
    """One security's terms from its current auctions (at least one; all with one CUSIP)."""
    if not auctions:
        raise ValueError("no auctions")
    by_issue = sorted(auctions, key=lambda a: (a.issue_date or date.max, a.source_key))
    originals = [a for a in by_issue if not a.reopening]
    orig = originals[0] if originals else None
    ref = orig or by_issue[0]
    v: dict = {}
    p: dict = {}
    checks: list[str] = []

    def pub(name, value, a: Auction | None, td_field: str):
        v[name] = value
        if value is not None and a is not None:
            p[name] = f"published: {SOURCE} {a.source_key} {td_field}"

    def der(name, value, rule: str):
        v[name] = value
        if value is not None:
            p[name] = f"derived: {rule}"

    if len({a.cusip for a in auctions}) != 1:
        raise ValueError("auctions of more than one CUSIP")
    for attr in ("maturity_date", "coupon_rate", "inst_type", "frn_spread"):
        seen = {getattr(a, attr) for a in auctions}
        if len(seen) > 1:
            checks.append(f"auctions disagree on {attr}: {sorted(map(str, seen))}")
    if len(originals) > 1:
        checks.append(f"{len(originals)} original (non-reopening) auctions")
    if orig is None:
        checks.append("original auction not loaded: original issue terms from a reopening")

    pub("cusip", ref.cusip, ref, "cusip")
    v["inst_type"], v["security_type"] = ref.inst_type, ref.security_type
    p["security_type"] = f"published: {SOURCE} {ref.source_key} type"
    v["cmb"] = any(a.cmb for a in auctions)
    if orig:
        pub("term", orig.term, orig, "term")
        pub("original_term", orig.security_term, orig, "securityTerm")
        pub("announcement_date", orig.announcement_date, orig, "announcementDate")
        pub("auction_date", orig.auction_date, orig, "auctionDate")
        pub("issue_date", orig.issue_date, orig, "issueDate")
    else:
        pub("original_term", ref.original_term, ref, "originalSecurityTerm")
        # A reopening's original term is the program when it's whole ("26-Week", "30-Year").
        parts = [(n, u) for n, u in TERM_PART.findall(ref.original_term or "") if n != "0"]
        if len(parts) == 1:
            der("term", f"{parts[0][0]}-{parts[0][1]}", "a reopening's original term")
        else:
            v["term"] = None
            checks.append("term unknown until the original auction is loaded")
        for name in ("announcement_date", "auction_date"):
            v[name] = None
        pub("issue_date", ref.original_issue_date, ref, "originalIssueDate")
    pub("maturity_date", ref.maturity_date, ref, "maturityDate")

    sec = ref.security_type
    if sec == "bill":
        v["dated_date"] = None
    elif orig and orig.dated_date:
        pub("dated_date", orig.dated_date, orig, "datedDate")
    elif ref.original_dated_date:
        pub("dated_date", ref.original_dated_date, ref, "originalDatedDate")
    else:
        pub("dated_date", ref.dated_date, ref, "datedDate")
    pub("coupon_rate", ref.coupon_rate, ref, "interestRate")
    if sec in ("note", "bond", "tips") and ref.coupon_rate is None:
        checks.append("no coupon rate")

    # Coupons and accrual.
    der("day_count", DAY_COUNT[sec], {"bill": "bills: actual/360 discount", "frn": "FRNs accrue actual/360"}.get(
        sec, "notes, bonds and TIPS: actual/actual (ICMA)"))
    freq = FREQUENCY.get(ref.frequency or "")
    if sec == "bill":
        der("coupon_frequency", 0, "bills pay no coupon")
    elif freq:
        pub("coupon_frequency", freq, ref, "interestPaymentFrequency")
    else:
        der("coupon_frequency", 4 if sec == "frn" else 2, "FRNs pay quarterly" if sec == "frn" else "semiannual")
        checks.append(f"interestPaymentFrequency {ref.frequency!r} not read; assumed")
    first = (orig or ref).first_coupon_date
    v["first_coupon_date"] = v["first_period_type"] = v["penultimate_coupon_date"] = None
    v["end_of_month"] = None
    if sec != "bill":
        src = orig or ref
        pub("first_coupon_date", first, src, "firstInterestPaymentDate")
        der("end_of_month", _eom(ref.maturity_date),
            "pays on the last day of each coupon month when it matures on a month's last day")
        f = v["coupon_frequency"]
        start = v.get("dated_date")
        if first is None or start is None:
            checks.append("no first coupon or dated date: schedule not checked")
        else:
            regular = regular_schedule(ref.maturity_date, f, start)
            if first not in regular:
                checks.append(f"first coupon {first} isn't on the regular schedule back from maturity")
            before_last = [d for d in regular if first <= d < ref.maturity_date]
            der("penultimate_coupon_date", before_last[-1] if before_last else None,
                f"the regular coupon date {12 // f} months before maturity (end-of-month rule)")
            step = 12 // f
            prior = add_months(first, -step, _eom(ref.maturity_date))
            derived_type = "Normal" if prior == start else ("Short" if prior < start else "Long")
            if src.first_period_type:
                pub("first_period_type", src.first_period_type, src, "firstInterestPeriod")
                if src.first_period_type != derived_type:
                    checks.append(f"first period published {src.first_period_type}, schedule says {derived_type}")
            else:
                der("first_period_type", derived_type, "the first coupon date against the regular schedule")

    # Redemption, settlement, calls, stripping.
    der("redemption", Decimal(100), "100 per 100 at maturity (TIPS: inflation-adjusted, floored at par)")
    der("settlement_days", 1, "T+1, the Treasury market's standard settlement")
    calendar_src = "SIFMA-US: U.S. Government Securities Business Days; a payment on a holiday moves to the next"
    der("calendar", "SIFMA-US", calendar_src)
    pub("callable", ref.callable, ref, "callable")
    pub("call_date", (orig or ref).call_date, orig or ref, "callDate")
    pub("called_date", max((a.called_date for a in auctions if a.called_date), default=None), ref, "calledDate")
    pub("strippable", ref.strippable, ref, "strippable")
    pub("corpus_cusip", next((a.corpus_cusip for a in by_issue if a.corpus_cusip), None),
        next((a for a in by_issue if a.corpus_cusip), None), "corpusCusip")

    # TIPS and FRNs.
    v["tips_base_cpi"] = v["cpi_base_period"] = v["frn_spread"] = v["frn_index"] = None
    if sec == "tips":
        pub("tips_base_cpi", ref.tips_base_cpi, ref, "refCpiOnDatedDate")
        pub("cpi_base_period", ref.cpi_base_period, ref, "cpiBaseReferencePeriod")
        if ref.tips_base_cpi is None:
            checks.append("TIPS without a reference CPI on the dated date")
    if sec == "frn":
        pub("frn_spread", (orig or ref).frn_spread, orig or ref, "spread")
        der("frn_index", FRN_INDEX, "FRNs are indexed to the 13-week bill auction's high rate")

    return Terms(v, p, checks)


# --- names -------------------------------------------------------------------


def coupon_label(rate: Decimal) -> str:
    """A decimal coupon as the percent traders say: 0.0425 -> "4.25", 0.05125 -> "5.125", 0.04 -> "4.00"."""
    pct = (rate * 100).normalize()
    if pct.as_tuple().exponent > -2:
        pct = pct.quantize(Decimal("0.01"))
    return format(pct, "f")


def short_name(t: dict) -> str:
    """UST-4.25-2035-08-15, UST-B-2026-12-24, UST-TII-1.875-2035-07-15, UST-FRN-2028-07-31 (Bill, 2026-10-06)."""
    mat = t["maturity_date"].isoformat()
    sec = t["security_type"]
    if sec == "bill":
        return f"UST-B-{mat}"
    if sec == "frn":
        return f"UST-FRN-{mat}"
    rate = t.get("coupon_rate")
    label = coupon_label(rate) if rate is not None else "X"
    return f"UST-TII-{label}-{mat}" if sec == "tips" else f"UST-{label}-{mat}"


KIND = {"bill": "Bill", "note": "Note", "bond": "Bond", "tips": "TIPS", "frn": "FRN"}


def describe(t: dict) -> str:
    sec = t["security_type"]
    kind = ("Cash Management Bill" if t.get("cmb") else "Bill") if sec == "bill" else KIND[sec]
    rate = f"{coupon_label(t['coupon_rate'])}% " if t.get("coupon_rate") is not None else ""
    term = f", {t['term']}" if t.get("term") else ""
    return f"US Treasury {rate}{kind} due {t['maturity_date'].isoformat()} (CUSIP {t['cusip']}{term})"
