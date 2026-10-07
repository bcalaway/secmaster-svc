"""The security master's tables (mkt-data's docs/phase-2.md, Part B step 4).

Instruments are known everywhere by a hidden integer `sec_id` and a short
readable name. Every change here needs a matching Alembic migration
(`migrations/versions/`); `tests/test_migrations.py` fails if they disagree.

Nothing is deleted. A name, identifier or note the seed file stops listing
gets `removed_at`, so a past mapping can still be explained, and lookups
skip it.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instrument"

    sec_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # cmt_yield; ust_bill, ust_note, ust_bond, ust_tips, ust_frn
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


# JSONB on Postgres (the hub); plain JSON on SQLite (tests).
JSON_DOC = JSON().with_variant(JSONB(), "postgresql")


class SecurityTerms(Base):
    """A Treasury security's terms (mkt-data's docs/phase-3.md, step 3), with history.

    One current row per security (superseded_at null). When what we know
    changes (results replace an announcement, a published field is revised),
    the row is superseded and a new one recorded, so any past view can be
    rebuilt. `provenance` says where each term came from: "published:
    TD-SECURITIES <record key> <field>" or "derived: <rule>". `checks` lists
    anything that doesn't add up (a first coupon off the regular schedule, an
    original auction not loaded yet). Rates are decimals (0.0425).
    """

    __tablename__ = "security_terms"
    __table_args__ = (
        Index("uq_security_terms_current", "sec_id", unique=True,
              postgresql_where=text("superseded_at IS NULL"), sqlite_where=text("superseded_at IS NULL")),
        Index("ix_security_terms_maturity", "maturity_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    cusip: Mapped[str] = mapped_column(String(9))
    security_type: Mapped[str] = mapped_column(String(10))  # bill, note, bond, tips, frn
    cmb: Mapped[bool] = mapped_column(Boolean)
    term: Mapped[str | None] = mapped_column(String(20))  # the auction program: 10-Year, 26-Week
    original_term: Mapped[str | None] = mapped_column(String(30))  # exact: 5-Year 2-Month
    announcement_date: Mapped[date | None] = mapped_column(Date)
    auction_date: Mapped[date | None] = mapped_column(Date)
    issue_date: Mapped[date | None] = mapped_column(Date)
    dated_date: Mapped[date | None] = mapped_column(Date)  # accrual start
    maturity_date: Mapped[date] = mapped_column(Date)
    coupon_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    coupon_frequency: Mapped[int] = mapped_column(SmallInteger)  # 0 bills, 2, 4 FRNs
    day_count: Mapped[str] = mapped_column(String(16))
    first_coupon_date: Mapped[date | None] = mapped_column(Date)
    first_period_type: Mapped[str | None] = mapped_column(String(10))  # Normal, Short, Long
    penultimate_coupon_date: Mapped[date | None] = mapped_column(Date)
    end_of_month: Mapped[bool | None] = mapped_column(Boolean)
    redemption: Mapped[Decimal] = mapped_column(Numeric)
    settlement_days: Mapped[int] = mapped_column(SmallInteger)
    calendar: Mapped[str] = mapped_column(String(20))
    callable: Mapped[bool | None] = mapped_column(Boolean)
    call_date: Mapped[date | None] = mapped_column(Date)
    called_date: Mapped[date | None] = mapped_column(Date)
    strippable: Mapped[bool | None] = mapped_column(Boolean)
    corpus_cusip: Mapped[str | None] = mapped_column(String(9))
    tips_base_cpi: Mapped[Decimal | None] = mapped_column(Numeric)
    cpi_base_period: Mapped[str | None] = mapped_column(String(20))
    frn_spread: Mapped[Decimal | None] = mapped_column(Numeric)
    frn_index: Mapped[str | None] = mapped_column(String(40))
    provenance: Mapped[dict] = mapped_column(JSON_DOC)
    checks: Mapped[list] = mapped_column(JSON_DOC)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Auction(Base):
    """One auction of a Treasury security: its original issue or a reopening.

    Typed from mkt-data's near-raw record (`source_key`: CUSIP/issue date), with
    the record's fields as published in `fields` and its lineage. Results fill
    in on auction day (the row is updated); a record mkt-data drops gets
    `removed_at`. Rates are decimals; prices per 100; amounts in dollars.
    """

    __tablename__ = "auction"
    __table_args__ = (
        Index("uq_auction_key", "source", "source_key", unique=True),
        Index("ix_auction_sec_id", "sec_id"),
        Index("ix_auction_period", "source", "period"),
        Index("ix_auction_term_date", "security_type", "term", "auction_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    source: Mapped[str] = mapped_column(String(20))  # TD-SECURITIES
    period: Mapped[str] = mapped_column(String(10))  # mkt-data's period: month of auction
    source_key: Mapped[str] = mapped_column(String(40))
    cusip: Mapped[str] = mapped_column(String(9))
    security_type: Mapped[str] = mapped_column(String(10))
    reopening: Mapped[bool] = mapped_column(Boolean)
    term: Mapped[str | None] = mapped_column(String(20))  # the program auctioned: 13-Week, 30-Year
    security_term: Mapped[str | None] = mapped_column(String(30))
    announcement_date: Mapped[date | None] = mapped_column(Date)
    auction_date: Mapped[date | None] = mapped_column(Date)
    issue_date: Mapped[date | None] = mapped_column(Date)
    offering_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    total_tendered: Mapped[Decimal | None] = mapped_column(Numeric)
    total_accepted: Mapped[Decimal | None] = mapped_column(Numeric)
    bid_to_cover: Mapped[Decimal | None] = mapped_column(Numeric)
    high_yield: Mapped[Decimal | None] = mapped_column(Numeric)
    high_discount_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    high_investment_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    high_discount_margin: Mapped[Decimal | None] = mapped_column(Numeric)
    high_price: Mapped[Decimal | None] = mapped_column(Numeric)
    price_per_100: Mapped[Decimal | None] = mapped_column(Numeric)
    accrued_interest_per_1000: Mapped[Decimal | None] = mapped_column(Numeric)
    adjusted_accrued_interest_per_1000: Mapped[Decimal | None] = mapped_column(Numeric)
    index_ratio_on_issue_date: Mapped[Decimal | None] = mapped_column(Numeric)
    ref_cpi_on_issue_date: Mapped[Decimal | None] = mapped_column(Numeric)
    frn_index_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    frn_index_determination_date: Mapped[date | None] = mapped_column(Date)
    currently_outstanding: Mapped[Decimal | None] = mapped_column(Numeric)
    fields: Mapped[dict] = mapped_column(JSON_DOC)
    record_id: Mapped[int] = mapped_column(Integer)  # mkt-data's record.id
    capture_id: Mapped[int] = mapped_column(Integer)  # mkt-data's capture.id
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourcePeriod(Base):
    """Watermark: the newest mkt-data capture each source's period was loaded from."""

    __tablename__ = "source_period"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)
    capture_id: Mapped[int] = mapped_column(Integer)
    records: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UntypedRecord(Base):
    """A near-raw record the load couldn't read as a security: reported, not loaded."""

    __tablename__ = "untyped_record"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    period: Mapped[str] = mapped_column(String(10))
    problem: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoadRun(Base):
    """Each load from mkt-data: when, how it went, and what it did (metrics and the job's answer)."""

    __tablename__ = "load_run"
    __table_args__ = (CheckConstraint("outcome IN ('ok', 'error')", name="ck_load_run_outcome"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(8))
    detail: Mapped[str] = mapped_column(Text)  # JSON summary, or the error


class Strip(Base):
    """A Treasury STRIPS instrument's own facts (mkt-data's docs/phase-3.md, step 3c).

    One row per strip instrument, kept in line with its sources on every load:
    principal or interest, TIPS or nominal, the date it pays, and the security
    it came from (principal: its security; interest: the security whose record
    introduced the date). `provenance` and `checks` as for security_terms.
    """

    __tablename__ = "strip"

    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"), primary_key=True)
    cusip: Mapped[str] = mapped_column(String(9))
    kind: Mapped[str] = mapped_column(String(10))  # principal | interest
    tips: Mapped[bool] = mapped_column(Boolean)
    payment_date: Mapped[date | None] = mapped_column(Date)
    underlying_cusip: Mapped[str | None] = mapped_column(String(9))
    underlying_sec_id: Mapped[int | None] = mapped_column(ForeignKey("instrument.sec_id"))
    provenance: Mapped[dict] = mapped_column(JSON_DOC)
    checks: Mapped[list] = mapped_column(JSON_DOC)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrippedAmount(Base):
    """A strippable security's amounts at a month-end, from the MSPD's stripped-securities table.

    Dollars (the MSPD prints thousands). One row per MSPD line (`source_key`:
    principal STRIPS CUSIP/record date); the table's subtotal lines aren't kept.
    """

    __tablename__ = "stripped_amount"
    __table_args__ = (
        Index("uq_stripped_amount_key", "source_key", unique=True),
        Index("ix_stripped_amount_underlying", "underlying_cusip", "record_date"),
        Index("ix_stripped_amount_period", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(10))
    source_key: Mapped[str] = mapped_column(String(40))
    strip_cusip: Mapped[str] = mapped_column(String(9))
    underlying_cusip: Mapped[str] = mapped_column(String(9))
    record_date: Mapped[date] = mapped_column(Date)
    outstanding: Mapped[Decimal | None] = mapped_column(Numeric)
    unstripped: Mapped[Decimal | None] = mapped_column(Numeric)
    stripped: Mapped[Decimal | None] = mapped_column(Numeric)
    reconstituted: Mapped[Decimal | None] = mapped_column(Numeric)
    fields: Mapped[dict] = mapped_column(JSON_DOC)
    record_id: Mapped[int] = mapped_column(Integer)
    capture_id: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FigiLookup(Base):
    """What OpenFIGI said about a CUSIP, so each is asked once (app/figi.py)."""

    __tablename__ = "figi_lookup"

    cusip: Mapped[str] = mapped_column(String(9), primary_key=True)
    sec_id: Mapped[int] = mapped_column(ForeignKey("instrument.sec_id"))
    outcome: Mapped[str] = mapped_column(String(10))  # found | not_found | error
    figi: Mapped[str | None] = mapped_column(String(12))
    composite_figi: Mapped[str | None] = mapped_column(String(12))
    ticker: Mapped[str | None] = mapped_column(String(60))
    detail: Mapped[dict | None] = mapped_column(JSON_DOC)
    looked_up_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CpiMonth(Base):
    """CPI-U (CUUR0000SA0, not seasonally adjusted) by month, from mkt-data's BLS-CPI observations."""

    __tablename__ = "cpi_month"

    month: Mapped[date] = mapped_column(Date, primary_key=True)  # first of the month
    value: Mapped[Decimal] = mapped_column(Numeric)
    observation_id: Mapped[int] = mapped_column(Integer)
    capture_id: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReferenceCpi(Base):
    """Treasury's daily reference CPI for TIPS (app/tips.py), rebuilt whenever a CPI month changes."""

    __tablename__ = "reference_cpi"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric)
    method: Mapped[str] = mapped_column(String(10))  # published | fallback
