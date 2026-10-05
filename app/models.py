"""The security master's tables (mkt-data's docs/phase-2.md, Part B step 4).

Instruments are known everywhere by a hidden integer `sec_id` and a short
readable name. Every change here needs a matching Alembic migration
(`migrations/versions/`); `tests/test_migrations.py` fails if they disagree.

Nothing is deleted. A name, identifier or note the seed file stops listing
gets `removed_at`, so a past mapping can still be explained, and lookups
skip it.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instrument"

    sec_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # cmt_yield in phase 2; bond, future, fixing later
    currency: Mapped[str] = mapped_column(String(3))
    country: Mapped[str] = mapped_column(String(2))
    curve: Mapped[str | None] = mapped_column(String(20))  # UST
    tenor: Mapped[str | None] = mapped_column(String(10))  # ISO 8601 duration: P10Y, P6W
    calendar: Mapped[str] = mapped_column(String(20))  # a calendar-svc name: SIFMA-US
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentName(Base):
    """An instrument's short name (exactly one current) and its aliases.

    A rename keeps the old name as an alias, so links and saved views that
    use it still resolve.
    """

    __tablename__ = "instrument_name"
    __table_args__ = (
        # A name means one instrument, current or not.
        Index("uq_instrument_name_name", "name", unique=True),
        # One current short name per instrument.
        Index(
            "uq_instrument_name_short", "sec_id", unique=True,
            postgresql_where=text("kind = 'short' AND removed_at IS NULL"),
            sqlite_where=text("kind = 'short' AND removed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    name: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10))  # short | alias
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Identifier(Base):
    """A source's key for an instrument: (scheme, value) -> sec_id.

    For CMTs the scheme is an mkt-data source name and the value that
    source's key (UST-PAR / BC_10YEAR), so quote-svc maps an observation
    directly. valid_from and valid_to bound when the mapping holds (open
    when null); CMT identifiers hold always.
    """

    __tablename__ = "identifier"
    __table_args__ = (
        Index(
            "uq_identifier_current", "scheme", "value", "valid_from", unique=True,
            # An open valid_from (NULL) must collide too; Postgres 15+.
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("removed_at IS NULL"), sqlite_where=text("removed_at IS NULL"),
        ),
        Index("ix_identifier_sec_id", "sec_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    scheme: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(60))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstrumentNote(Base):
    """A dated note that explains a series without splitting it (a method change, a gap)."""

    __tablename__ = "instrument_note"
    __table_args__ = (
        Index(
            "uq_instrument_note_key", "sec_id", "key", unique=True,
            postgresql_where=text("removed_at IS NULL"), sqlite_where=text("removed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    key: Mapped[str] = mapped_column(String(40))
    on_date: Mapped[date | None] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SeedRun(Base):
    """Each application of a seed file, for the metrics and the job's answer."""

    __tablename__ = "seed_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seed: Mapped[str] = mapped_column(String(40))  # file stem: cmt
    sha256: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(10))  # ok | error
    summary: Mapped[str] = mapped_column(Text, server_default="")  # JSON
