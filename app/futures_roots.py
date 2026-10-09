"""Find Bloomberg roots for futures products through OpenFIGI's search (mkt-data's docs/phase-4.md, step 2c).

The products in seeds/futures_candidates.toml have no Bloomberg code on their CME pages, and OpenFIGI's
mapping can't look a contract up by CME's code (step 2b: "No identifier found" for every one). So each
candidate is searched by its queries, filtered to futures in its market sector (Curncy for FX, Comdty
for rates) on its Bloomberg exchange (CME or CBT), and the futures found are grouped by root (the ticker
without its month and year) and name (without its month). The job's answer lists the groups per
candidate, most contracts first, for a person to read: a root goes into seeds/futures.toml, in a
reviewed PR, only once its name is plainly that product. Nothing is stored. Run by hand (the DAG
secmaster_svc__futures_roots, POST /jobs/futures-roots); `only` limits it to some CME codes.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from app import figi
from app.futures_figi import SECTOR, parse_ticker, product_name

PATH = Path(__file__).resolve().parent.parent / "seeds" / "futures_candidates.toml"
GROUPS = 6  # groups listed per candidate


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    cme_code: str
    name: str
    kind: str
    exch_code: str
    queries: tuple[str, ...]


def load(path: Path = PATH) -> list[Candidate]:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise CandidateError(f"futures candidates: not valid TOML: {e}") from None
    out, seen = [], set()
    for item in raw.get("candidates", []):
        c = Candidate(str(item.get("cme_code", "")), str(item.get("name", "")), str(item.get("kind", "")),
                      str(item.get("exch_code", "")), tuple(str(q) for q in item.get("queries") or []))
        if not c.cme_code or c.cme_code in seen or c.kind not in SECTOR or not c.exch_code or not c.queries:
            raise CandidateError(f"futures candidates: bad or repeated entry {item}")
        seen.add(c.cme_code)
        out.append(c)
    return out


def group(rows: list[dict], exch_code: str) -> list[dict]:
    """Futures rows on the exchange, grouped by root and name: [{root, name, contracts, example}], most first."""
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.get("exchCode") != exch_code:
            continue
        p = parse_ticker(r.get("ticker"))
        if p is None:
            continue
        key = (p[0], product_name(r.get("name")))
        g = groups.setdefault(key, {"root": key[0], "name": key[1], "contracts": 0,
                                    "example": f"{r.get('ticker')} {r.get('marketSector')} ({r.get('name')})"})
        g["contracts"] += 1
    return sorted(groups.values(), key=lambda g: (-g["contracts"], g["root"]))


def run(api_key: str | None, only: list[str] | None = None, searcher=None, candidates=None) -> dict:
    searcher = searcher or figi.search
    cands = candidates if candidates is not None else load()
    if only:
        cands = [c for c in cands if c.cme_code in set(only)]
    out = {"searched": 0, "with_key": bool(api_key), "candidates": {}}
    for c in cands:
        rows: list[dict] = []
        said = []
        for q in c.queries:
            got = searcher({"query": q, "securityType2": "Future", "marketSecDes": SECTOR[c.kind]}, api_key, pages=1)
            out["searched"] += 1
            said.append(f"{q}: {len(got)}")
            rows += got
        seen, unique = set(), []
        for r in rows:  # the same future found by two queries counts once
            if r.get("figi") not in seen:
                seen.add(r.get("figi"))
                unique.append(r)
        groups = group(unique, c.exch_code)
        out["candidates"][c.cme_code] = {"name": c.name, "exch_code": c.exch_code, "sector": SECTOR[c.kind],
                                         "queries": said, "groups": groups[:GROUPS], "more_groups": max(0, len(groups) - GROUPS)}
    return out
