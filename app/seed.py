"""Apply the seed files in seeds/ to the security master (phase 2, Part B step 4).

A seed file lists instruments that come from no feed (the Treasury CMTs), with
their short names, aliases, identifiers and notes. Applying it is idempotent:

- An instrument is found by any of its listed names, so a rename (new short
  name, old one kept as an alias) keeps its sec_id. Otherwise it's created.
- Attributes are updated in place when they change.
- Names, identifiers and notes the file no longer lists get `removed_at`;
  nothing is deleted. Instruments the file doesn't list are never touched.
- An identifier or name that belongs to an instrument outside the file is a
  conflict: the run fails and changes nothing.

Runs at every container start (start.sh, after migrations), on
POST /jobs/seed, and from the manual DAG secmaster_svc__seed. Each run is
recorded in seed_run.
"""

import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Identifier, Instrument, InstrumentName, InstrumentNote, SeedRun

SEEDS = Path(__file__).resolve().parent.parent / "seeds"
# Seed files that aren't instrument seeds: the futures product seed (app/futures_seed.py) and the
# futures root candidates (app/futures_roots.py).
NOT_INSTRUMENT_SEEDS = frozenset({"futures.toml", "futures_candidates.toml"})

NAME = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{1,39}$")
TENOR = re.compile(r"^P(\d+(\.\d+)?[DWMY])+$")
SCHEME = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,19}$")
ATTRS = ("type", "currency", "country", "curve", "tenor", "calendar", "description")


class SeedError(ValueError):
    pass


@dataclass(frozen=True)
class Note:
    key: str
    on_date: date | None
    text: str


@dataclass(frozen=True)
class SeedInstrument:
    short_name: str
    aliases: tuple[str, ...]
    attrs: dict
    identifiers: tuple[tuple[str, str], ...]  # (scheme, value)
    notes: tuple[Note, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return (self.short_name, *self.aliases)


@dataclass
class Seed:
    name: str
    sha256: str
    instruments: list[SeedInstrument] = field(default_factory=list)


def _date(v, where: str) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise SeedError(f"{where}: bad date {v!r}") from None


def _notes(raw: list, where: str) -> list[Note]:
    out = []
    for n in raw or []:
        if not n.get("key") or not n.get("text"):
            raise SeedError(f"{where}: a note needs a key and text")
        out.append(Note(str(n["key"]), _date(n.get("date"), where), str(n["text"])))
    return out


def parse(body: bytes, name: str) -> Seed:
    """Parse and check a seed file. Raises SeedError with the reason."""
    try:
        raw = tomllib.loads(body.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SeedError(f"{name}: not valid TOML: {e}") from None
    defaults = raw.get("defaults", {})
    shared = _notes(raw.get("notes", []), f"{name} [[notes]]")
    seed = Seed(name=name, sha256=hashlib.sha256(body).hexdigest())
    seen_names: dict[str, str] = {}
    seen_ids: dict[tuple[str, str], str] = {}
    for i, item in enumerate(raw.get("instruments", [])):
        short = str(item.get("short_name", "")).upper()
        where = f"{name} instrument {short or i}"
        if not NAME.match(short):
            raise SeedError(f"{where}: bad short_name")
        aliases = tuple(str(a).upper() for a in item.get("aliases", []))
        for n in (short, *aliases):
            if not NAME.match(n):
                raise SeedError(f"{where}: bad name {n!r}")
            if n in seen_names:
                raise SeedError(f"{where}: name {n} is also {seen_names[n]}'s")
            seen_names[n] = short
        attrs = {k: item.get(k, defaults.get(k)) for k in ATTRS}
        attrs["description"] = attrs["description"] or ""
        for k in ("type", "currency", "country", "calendar"):
            if not attrs[k]:
                raise SeedError(f"{where}: missing {k}")
        if attrs["tenor"] and not TENOR.match(attrs["tenor"]):
            raise SeedError(f"{where}: tenor {attrs['tenor']!r} isn't an ISO 8601 duration")
        ids = []
        for scheme, value in sorted((item.get("identifiers") or {}).items()):
            if not SCHEME.match(scheme) or not str(value):
                raise SeedError(f"{where}: bad identifier {scheme}={value!r}")
            k = (scheme, str(value))
            if k in seen_ids:
                raise SeedError(f"{where}: identifier {scheme}={value} is also {seen_ids[k]}'s")
            seen_ids[k] = short
            ids.append(k)
        notes = shared + _notes(item.get("notes", []), where)
        keys = [n.key for n in notes]
        if len(keys) != len(set(keys)):
            raise SeedError(f"{where}: duplicate note key")
        seed.instruments.append(SeedInstrument(short, aliases, attrs, tuple(ids), tuple(notes)))
    if not seed.instruments:
        raise SeedError(f"{name}: no instruments")
    return seed


def load(path: Path) -> Seed:
    return parse(path.read_bytes(), path.stem)


def _apply(s: Session, seed: Seed, now: datetime) -> dict:
    out = dict.fromkeys(
        ("created", "updated", "names_added", "names_removed", "identifiers_added",
         "identifiers_removed", "notes_added", "notes_updated", "notes_removed"), 0)

    names = {n.name: n for n in s.scalars(select(InstrumentName))}
    listed_ids: dict[str, int] = {}
    for item in seed.instruments:
        owners = {names[n].sec_id for n in item.names if n in names and names[n].removed_at is None}
        if len(owners) > 1:
            raise SeedError(f"{item.short_name}: its names belong to different instruments ({sorted(owners)})")
        for n in item.names:
            if n in names and names[n].removed_at is not None and (not owners or names[n].sec_id not in owners):
                raise SeedError(f"{item.short_name}: name {n} was another instrument's and can't be reused")
        if owners:
            inst = s.get(Instrument, owners.pop())
        else:
            inst = Instrument(**item.attrs, status="active", created_at=now, updated_at=now)
            s.add(inst)
            s.flush()
            out["created"] += 1
        listed_ids[item.short_name] = inst.sec_id
        changed = [k for k in ATTRS if getattr(inst, k) != item.attrs[k]] + (["status"] if inst.status != "active" else [])
        if changed:
            for k in ATTRS:
                setattr(inst, k, item.attrs[k])
            inst.status = "active"
            inst.updated_at = now
            out["updated"] += 1

    seed_secs = set(listed_ids.values())

    # Names: retire and demote first, so the one-short-name index never sees two.
    want_names = {}
    for item in seed.instruments:
        want_names[item.short_name] = (listed_ids[item.short_name], "short")
        for a in item.aliases:
            want_names[a] = (listed_ids[item.short_name], "alias")
    for row in s.scalars(select(InstrumentName).where(InstrumentName.sec_id.in_(seed_secs),
                                                      InstrumentName.removed_at.is_(None))):
        want = want_names.get(row.name)
        if want is None:
            row.removed_at = now
            out["names_removed"] += 1
        elif row.kind == "short" and want[1] == "alias":
            row.kind = "alias"
    s.flush()
    for name, (sec_id, kind) in want_names.items():
        row = names.get(name)
        if row is None:
            s.add(InstrumentName(sec_id=sec_id, name=name, kind=kind, created_at=now))
            out["names_added"] += 1
        else:
            if row.removed_at is not None:
                row.removed_at = None
                out["names_added"] += 1
            row.kind = kind
    s.flush()

    # Identifiers: an identifier current on an instrument outside the file is a conflict.
    want_ids = {(sc, v): listed_ids[item.short_name] for item in seed.instruments for sc, v in item.identifiers}
    current = s.scalars(select(Identifier).where(Identifier.removed_at.is_(None), Identifier.valid_from.is_(None))).all()
    for row in current:
        owner = want_ids.get((row.scheme, row.value))
        if row.sec_id not in seed_secs:
            if owner is not None:
                raise SeedError(f"identifier {row.scheme}={row.value} belongs to sec_id {row.sec_id}, outside this seed")
            continue
        if owner != row.sec_id:
            row.removed_at = now
            out["identifiers_removed"] += 1
    s.flush()
    have = {(r.scheme, r.value) for r in current if r.removed_at is None}
    for (scheme, value), sec_id in sorted(want_ids.items()):
        if (scheme, value) not in have:
            s.add(Identifier(sec_id=sec_id, scheme=scheme, value=value, created_at=now))
            out["identifiers_added"] += 1

    # Notes, by key per instrument.
    for item in seed.instruments:
        sec_id = listed_ids[item.short_name]
        rows = {n.key: n for n in s.scalars(select(InstrumentNote).where(
            InstrumentNote.sec_id == sec_id, InstrumentNote.removed_at.is_(None)))}
        want = {n.key: n for n in item.notes}
        for key, row in rows.items():
            if key not in want:
                row.removed_at = now
                out["notes_removed"] += 1
        for key, n in want.items():
            row = rows.get(key)
            if row is None:
                s.add(InstrumentNote(sec_id=sec_id, key=key, on_date=n.on_date, text=n.text, created_at=now))
                out["notes_added"] += 1
            elif (row.on_date, row.text) != (n.on_date, n.text):
                row.on_date, row.text = n.on_date, n.text
                out["notes_updated"] += 1
    s.flush()
    out["changed"] = any(v for v in out.values())
    out["instruments"] = len(seed.instruments)
    return out


def apply(s: Session, seed: Seed, now: datetime | None = None) -> dict:
    """Apply one parsed seed and record the run. Commits; raises SeedError on a conflict."""
    now = now or datetime.now(UTC)
    try:
        out = _apply(s, seed, now)
    except SeedError as e:
        s.rollback()
        s.add(SeedRun(seed=seed.name, sha256=seed.sha256, started_at=now, finished_at=datetime.now(UTC),
                      outcome="error", summary=json.dumps({"error": str(e)})))
        s.commit()
        raise
    summary = {"seed": seed.name, "sha256": seed.sha256} | out
    s.add(SeedRun(seed=seed.name, sha256=seed.sha256, started_at=now, finished_at=datetime.now(UTC),
                  outcome="ok", summary=json.dumps(summary)))
    s.commit()
    return summary


def apply_all(s: Session, directory: Path = SEEDS) -> list[dict]:
    """Apply every instrument seed (seeds/*.toml but futures.toml, which app/futures_load.py reads), in name
    order. Parses all first, so a bad file changes nothing."""
    seeds = [load(p) for p in sorted(directory.glob("*.toml")) if p.name not in NOT_INSTRUMENT_SEEDS]
    return [apply(s, seed) for seed in seeds]


def main() -> int:
    from app import db

    try:
        with db.session() as s:
            results = apply_all(s)
    except db.DatabaseNotConfigured:
        print("seed: no database configured, skipped")
        return 0
    except SeedError as e:
        print(f"seed failed: {e}", file=sys.stderr)
        return 1
    for r in results:
        print("seed:", json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
