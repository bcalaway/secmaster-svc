"""On-the-run aliases (mkt-data's docs/phase-3.md, step 3b; Bill, 2026-10-06).

`UST-10Y-OTR` names the most recently auctioned security of each Treasury
program, with history: it's an identifier (scheme OTR) with validity, so "the
10-year" on any past date resolves to that day's security.

- **Programs** follow TreasuryDirect's `term`, the program auctioned: a
  reopened 30-year is still "30-Year", and a 13-week bill auction (a reopening
  of an older 26- or 52-week bill) is "13-Week". Families: nominal notes and
  bonds (`UST-10Y-OTR`), TIPS (`UST-10Y-TII-OTR`), FRNs (`UST-2Y-FRN-OTR`) and
  bills (`UST-13W-OTR`). Cash management bills are left out.
- **When it switches:** on the new security's auction date, the market
  convention (the auction sets the coupon and the new issue becomes the
  benchmark). The `-ISSUED` variants (`UST-10Y-OTR-ISSUED`) switch on the issue
  date instead, for anything that needs a settled security with a price.
- **Validity** runs to the day before the next security in the program takes
  over, and never past the day before maturity. A program that stops being
  auctioned (the 30-year from 2001 to 2006, the 52-week bill from 2001 to
  2008, the 20-year after 1986) loses its on-the-run a year after its last
  auction rather than pointing at an ever-older security.

Pure functions; app/load.py syncs the result into `identifier` after each load.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from app.treasuries import term_iso

SCHEME = "OTR"
STALE_AFTER = timedelta(days=366)
FAMILY = {"note": "nominal", "bond": "nominal", "tips": "tips", "frn": "frn", "bill": "bill"}
SUFFIX = {"nominal": "", "tips": "-TII", "frn": "-FRN", "bill": ""}


@dataclass(frozen=True)
class Auctioned:
    sec_id: int
    security_type: str  # note, bond, tips, frn, bill
    term: str  # the program: 10-Year, 13-Week
    auction_date: date | None
    issue_date: date | None
    maturity_date: date


@dataclass(frozen=True)
class Interval:
    alias: str
    sec_id: int
    valid_from: date
    valid_to: date | None  # inclusive; None: still on the run


def alias_name(security_type: str, term: str, issued: bool = False) -> str | None:
    iso = term_iso(term)
    family = FAMILY.get(security_type)
    if not iso or family is None:
        return None
    return f"UST-{iso[1:]}{SUFFIX[family]}-OTR" + ("-ISSUED" if issued else "")


def _runs(events: list[tuple[date, Auctioned]]) -> list[tuple[date, date, Auctioned]]:
    """Consecutive auctions of one security collapse into a run: (start, last auction, the security)."""
    runs: list[list] = []
    for when, a in events:
        if runs and runs[-1][2].sec_id == a.sec_id:
            runs[-1][1] = when
        else:
            runs.append([when, when, a])
    return [tuple(r) for r in runs]


def intervals(auctions: list[Auctioned], today: date) -> list[Interval]:
    groups: dict[tuple[str, str], list[Auctioned]] = {}
    for a in auctions:
        if a.term and FAMILY.get(a.security_type):
            groups.setdefault((FAMILY[a.security_type], a.term), []).append(a)
    out: list[Interval] = []
    for members in groups.values():
        for issued in (False, True):
            dated = [((a.issue_date if issued else a.auction_date), a) for a in members]
            events = sorted(((d, a) for d, a in dated if d is not None),
                            key=lambda e: (e[0], e[1].issue_date or date.min, e[1].sec_id))
            runs = _runs(events)
            for i, (start, last, a) in enumerate(runs):
                end = a.maturity_date - timedelta(days=1)
                following = runs[i + 1][0] if i + 1 < len(runs) else today
                if following > last + STALE_AFTER:
                    end = min(end, last + STALE_AFTER)  # the program stopped being auctioned for a while
                if i + 1 < len(runs):
                    end = min(end, runs[i + 1][0] - timedelta(days=1))
                if end < start:
                    continue  # superseded the same day, or matured before it could be on the run
                current = i + 1 == len(runs) and end >= today
                out.append(Interval(alias_name(a.security_type, a.term, issued), a.sec_id, start,
                                    None if current else end))
    return sorted(out, key=lambda x: (x.alias, x.valid_from))
