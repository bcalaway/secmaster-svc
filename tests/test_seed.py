"""The seed job (app/seed.py): seeds/cmt.toml applied idempotently, with history."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db, seed
from app.models import Identifier, Instrument, InstrumentName, InstrumentNote, SeedRun

CMT = (Path(__file__).resolve().parent.parent / "seeds" / "cmt.toml").read_bytes()

SMALL = b"""
[defaults]
type = "cmt_yield"
currency = "USD"
country = "US"
curve = "UST"
calendar = "SIFMA-US"

[[notes]]
key = "method"
date = "2021-12-06"
text = "Method change."

[[instruments]]
short_name = "UST-10Y-CMT"
tenor = "P10Y"
identifiers = { UST-PAR = "BC_10YEAR", H15-TCM = "RIFLGFCY10_N.B" }

[[instruments]]
short_name = "UST-1.5M-CMT"
aliases = ["UST-6W-CMT"]
tenor = "P6W"
identifiers = { UST-PAR = "BC_1_5MONTH" }
"""


def _apply(body: bytes, name: str = "cmt") -> dict:
    with db.session() as s:
        return seed.apply(s, seed.parse(body, name))


def _count(model, *where) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_the_cmt_seed_file(migrated_db):
    out = _apply(CMT)
    assert out["instruments"] == 14 and out["created"] == 14 and out["changed"]
    assert out["identifiers_added"] == 36  # 14 UST-PAR + 11 H15-TCM + 11 FRED
    assert _count(InstrumentName, InstrumentName.kind == "alias") == 1  # UST-6W-CMT
    # The shared note lands on every instrument; the 20-year has three of its own.
    with db.session() as s:
        twenty = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == "UST-20Y-CMT"))
        keys = s.scalars(select(InstrumentNote.key).where(InstrumentNote.sec_id == twenty)).all()
    assert sorted(keys) == ["composite-before-2020", "gap-1987-1993", "h15-first", "par-curve-method-2021"]


def test_reapplying_changes_nothing(migrated_db):
    _apply(CMT)
    again = _apply(CMT)
    assert not again["changed"] and again["created"] == 0
    assert _count(Instrument) == 14 and _count(SeedRun) == 2


def test_a_rename_keeps_the_sec_id_and_the_old_name(migrated_db):
    _apply(SMALL)
    with db.session() as s:
        before = s.scalar(select(InstrumentName.sec_id).where(InstrumentName.name == "UST-10Y-CMT"))
    renamed = SMALL.replace(b'short_name = "UST-10Y-CMT"', b'short_name = "UST-10Y-YLD"\naliases = ["UST-10Y-CMT"]')
    out = _apply(renamed)
    assert out["created"] == 0 and out["names_added"] == 1
    with db.session() as s:
        rows = {r.name: r for r in s.scalars(select(InstrumentName).where(InstrumentName.sec_id == before))}
    assert rows["UST-10Y-YLD"].kind == "short" and rows["UST-10Y-CMT"].kind == "alias"


def test_dropped_rows_are_closed_not_deleted(migrated_db):
    _apply(SMALL)
    fewer = SMALL.replace(b', H15-TCM = "RIFLGFCY10_N.B"', b"").replace(b'aliases = ["UST-6W-CMT"]\n', b"")
    out = _apply(fewer)
    assert out["identifiers_removed"] == 1 and out["names_removed"] == 1
    assert _count(Identifier) == 3 and _count(Identifier, Identifier.removed_at.is_(None)) == 2
    assert _count(InstrumentName, InstrumentName.name == "UST-6W-CMT", InstrumentName.removed_at.is_not(None)) == 1


def test_attribute_and_note_changes_update_in_place(migrated_db):
    _apply(SMALL)
    changed = SMALL.replace(b'calendar = "SIFMA-US"', b'calendar = "FED"').replace(b"Method change.", b"Method changed.")
    out = _apply(changed)
    assert out["updated"] == 2 and out["notes_updated"] == 2 and out["created"] == 0
    with db.session() as s:
        assert set(s.scalars(select(Instrument.calendar))) == {"FED"}


def test_an_identifier_moves_between_listed_instruments(migrated_db):
    _apply(SMALL)
    moved = SMALL.replace(b'identifiers = { UST-PAR = "BC_1_5MONTH" }',
                          b'identifiers = { UST-PAR = "BC_1_5MONTH", FRED = "X" }')
    _apply(moved)
    swapped = moved.replace(b'FRED = "X"', b'FRED = "Y"')
    out = _apply(swapped)
    assert out["identifiers_added"] == 1 and out["identifiers_removed"] == 1


def test_a_conflict_with_an_unlisted_instrument_changes_nothing(migrated_db):
    _apply(SMALL)
    other = b"""
[[instruments]]
short_name = "OTHER"
type = "x"
currency = "USD"
country = "US"
calendar = "FED"
identifiers = { UST-PAR = "BC_10YEAR" }
"""
    with pytest.raises(seed.SeedError, match="outside this seed"):
        _apply(other, "other")
    assert _count(Instrument) == 2  # OTHER wasn't kept
    with db.session() as s:
        last = s.scalars(select(SeedRun).order_by(SeedRun.id.desc())).first()
    assert last.outcome == "error" and last.seed == "other"


@pytest.mark.parametrize("bad,reason", [
    (b"not toml [", "not valid TOML"),
    (b"[[instruments]]\nshort_name = \"bad name\"", "bad short_name"),
    (SMALL.replace(b'"P10Y"', b'"ten years"'), "ISO 8601"),
    (SMALL.replace(b'"BC_1_5MONTH"', b'"BC_10YEAR"'), "is also"),
    (SMALL.replace(b'aliases = ["UST-6W-CMT"]', b'aliases = ["UST-10Y-CMT"]'), "is also"),
    (SMALL.replace(b'date = "2021-12-06"', b'date = "2021-13-06"'), "bad date"),
])
def test_bad_files_are_refused(bad, reason):
    with pytest.raises(seed.SeedError, match=reason):
        seed.parse(bad, "t")


def test_main_without_a_database(monkeypatch, capsys):
    from app.config import Settings
    monkeypatch.setattr(db, "settings", Settings(database_url=None, postgres_password=None))
    db._engine.cache_clear()
    assert seed.main() == 0
    assert "skipped" in capsys.readouterr().out
    db._engine.cache_clear()
