"""Treasury STRIPS (mkt-data's docs/phase-3.md, step 3c; Bill, 2026-10-06): pure functions.

A strippable note, bond or TIPS can be split into zero-coupon pieces that
trade under their own CUSIPs:

- **Principal STRIPS** (one per security): its principal at maturity.
  TreasuryDirect gives the CUSIP on the security's record (`corpusCusip`),
  and the Monthly Statement of the Public Debt's stripped-securities table
  (MSPD, from 2001) lists every strippable security's principal STRIPS with
  the amounts stripped each month-end. Named `UST-SP-<maturity>` (TIPS:
  `UST-SP-TII-<maturity>`).
- **Interest STRIPS** (one per payment date, shared by every security that
  pays on it, nominal and TIPS separately): TreasuryDirect gives the CUSIP on
  the record of the security that first pays on that date (`tintCusip1`/`2`
  with their due dates). Named `UST-SI-<date>` (TIPS: `UST-SI-TII-<date>`).
  Linked to the security that introduced the date; every other security paying
  on it is fungible with it, which the analytics library can work out from
  the terms.

Two strips maturing the same day get the CUSIP appended to the second's name,
as for any short-name clash. app/load.py syncs these into instruments.
"""

from dataclasses import dataclass, field
from datetime import date

KINDS = {"principal": "ust_strip_principal", "interest": "ust_strip_interest"}


@dataclass
class StripSpec:
    cusip: str
    kind: str  # principal | interest
    tips: bool
    payment_date: date | None
    underlying_cusip: str | None  # principal: its security; interest: the security that introduced the date
    provenance: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)


def _d(text: str | None) -> date | None:
    if not text or text == "null":
        return None
    return date.fromisoformat(text[:10])


def from_security(cusip: str, security_type: str, maturity: date, corpus_cusip: str | None,
                  source_key: str) -> StripSpec | None:
    """A security's principal STRIPS, from its terms' corpus CUSIP."""
    if not corpus_cusip:
        return None
    return StripSpec(corpus_cusip, "principal", security_type == "tips", maturity, cusip,
                     {"cusip": f"published: TD-SECURITIES {source_key} corpusCusip",
                      "payment_date": "derived: the underlying security's maturity"})


def from_auction(cusip: str, security_type: str, maturity: date, fields: dict, source_key: str) -> list[StripSpec]:
    """Interest STRIPS CUSIPs a TreasuryDirect record introduces (tintCusip1/2 and their due dates)."""
    out = []
    pairs = [(fields.get("tintCusip1"), fields.get("tintCusip1DueDate")),
             (fields.get("tintCusip2"), fields.get("tintCusip2DueDate"))]
    listed = [(c.strip(), d) for c, d in pairs if c and c.strip()]
    for strip_cusip, due in listed:
        spec = StripSpec(strip_cusip, "interest", security_type == "tips", _d(due), cusip,
                         {"cusip": f"published: TD-SECURITIES {source_key} tintCusip"})
        if spec.payment_date is not None:
            spec.provenance["payment_date"] = f"published: TD-SECURITIES {source_key} tintCusipDueDate"
        elif len(listed) == 1:
            # The one new payment date a new security brings is its maturity's (seen on 2026-10's 3-year).
            spec.payment_date = maturity
            spec.provenance["payment_date"] = "derived: no due date published; the introducing security's maturity"
        else:
            spec.checks.append("no-payment-date: several interest STRIPS listed without due dates")
        out.append(spec)
    return out


def from_mspd(fields: dict, source_key: str) -> StripSpec | None:
    """A principal STRIPS line of the MSPD's stripped-securities table."""
    cusip, underlying = fields.get("cusip"), fields.get("security_class2_desc")
    if not cusip or cusip == "null" or not underlying or underlying == "null":
        return None
    tips = "Inflation" in (fields.get("security_class1_desc") or "")
    return StripSpec(cusip, "principal", tips, _d(fields.get("maturity_date")), underlying,
                     {"cusip": f"published: FD-MSPD-STRIPS {source_key} cusip",
                      "underlying_cusip": f"published: FD-MSPD-STRIPS {source_key} security_class2_desc"})


def merge(specs: list[StripSpec]) -> dict[str, StripSpec]:
    """One spec per strip CUSIP. TreasuryDirect's record wins; a disagreement goes to checks."""
    out: dict[str, StripSpec] = {}
    for spec in sorted(specs, key=lambda x: "FD-MSPD" in x.provenance.get("cusip", "")):
        have = out.get(spec.cusip)
        if have is None:
            out[spec.cusip] = spec
            continue
        for attr in ("kind", "tips", "payment_date", "underlying_cusip"):
            mine, theirs = getattr(have, attr), getattr(spec, attr)
            if theirs is not None and mine is not None and mine != theirs and attr != "underlying_cusip":
                have.checks.append(f"sources-disagree: on {attr}: {mine} and {theirs}")
            if mine is None and theirs is not None:
                setattr(have, attr, theirs)
        have.provenance = spec.provenance | have.provenance
        have.checks = sorted(set(have.checks + spec.checks))
    return out


def short_name(spec: StripSpec) -> str:
    code = "SP" if spec.kind == "principal" else "SI"
    tii = "-TII" if spec.tips else ""
    when = spec.payment_date.isoformat() if spec.payment_date else spec.cusip
    return f"UST-{code}{tii}-{when}"


def describe(spec: StripSpec, underlying: str | None) -> str:
    kind = "principal" if spec.kind == "principal" else "interest"
    tips = "TIPS " if spec.tips else ""
    due = spec.payment_date.isoformat() if spec.payment_date else "unknown date"
    of = f" of {underlying}" if underlying and spec.kind == "principal" else ""
    return f"US Treasury {tips}{kind} STRIPS{of} due {due} (CUSIP {spec.cusip})"
