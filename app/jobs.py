"""Job API for Airflow (ADR-0031 in nyc_pa_aws_gitops), plus read-only lookups.

Airflow's DAGs call these over the home-platform network; the work runs in
this container. Every endpoint requires `Authorization: Bearer
<AIRFLOW_TOKEN>` (other apps share the network), is idempotent (Airflow
retries) and answers with a JSON summary that shows in the task log.

The GET endpoints only read, and also accept READ_TOKEN. They're the same
lookups as the gRPC API, for checking by hand and for home-mcp later.
"""

import hmac
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app import db, load, securities, seed
from app.config import settings

router = APIRouter(prefix="/jobs")


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.airflow_token:
        raise HTTPException(503, "job API disabled: AIRFLOW_TOKEN isn't set")
    given = (authorization or "").removeprefix("Bearer ").strip().encode()
    if not hmac.compare_digest(given, settings.airflow_token.encode()):
        raise HTTPException(401, "bad or missing job token")


def require_read_token(authorization: str | None = Header(default=None)) -> None:
    """The Airflow token, or the read-only token. For GET endpoints only."""
    tokens = [t for t in (settings.airflow_token, settings.read_token) if t]
    if not tokens:
        raise HTTPException(503, "job API disabled: no AIRFLOW_TOKEN or READ_TOKEN set")
    given = (authorization or "").removeprefix("Bearer ").strip().encode()
    # Compare against every token (no early exit), so timing doesn't say which matched.
    matches = [hmac.compare_digest(given, t.encode()) for t in tokens]
    if not any(matches):
        raise HTTPException(401, "bad or missing token")


@router.post("/seed", dependencies=[Depends(require_token)])
def run_seed() -> dict:
    """Apply every seed file in seeds/ (also done at each start). 422 on a bad file or a conflict."""
    try:
        with db.session() as s:
            return {"seeds": seed.apply_all(s)}
    except seed.SeedError as e:
        raise HTTPException(422, f"seed failed: {e}") from None


def _upstream():
    from app.upstream import GrpcRecords

    return GrpcRecords(settings.mkt_data_grpc)


@router.post("/load", dependencies=[Depends(require_token)])
def run_load() -> dict:
    """Load Treasury securities from mkt-data's near-raw records: only periods whose newest capture moved.

    502 when mkt-data can't be read or the load fails part way (what's done is kept).
    """
    try:
        with db.session() as s, _upstream() as up:
            return load.run(s, up)
    except load.LoadError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/rebuild", dependencies=[Depends(require_token)])
def run_rebuild() -> dict:
    """Re-read every period from mkt-data (watermarks cleared). sec_ids are kept."""
    try:
        with db.session() as s, _upstream() as up:
            return load.rebuild(s, up)
    except load.LoadError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/futures", dependencies=[Depends(require_token)])
def run_futures() -> dict:
    """Generate futures products and contracts from seeds/futures.toml and calendar-svc's business days.

    502 when calendar-svc can't be read, the seed is bad or a rule can't be applied (nothing is changed).
    """
    from app import futures_load
    from app.calendars import GrpcCalendars

    try:
        with db.session() as s, GrpcCalendars(settings.calendar_svc_grpc) as cals:
            return futures_load.run(s, cals)
    except futures_load.FuturesError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/swap-check", dependencies=[Depends(require_token)])
def run_swap_check() -> dict:
    """Check the SPGMI swap curve files' stated conventions against swap_terms (app/swap_check.py).

    Reads mkt-data's curve records and calendar-svc's ISDA calendars; 502 if either can't be read.
    """
    from app import swap_check
    from app.calendars import CalendarUnavailable, GrpcCalendars
    from app.upstream import GrpcRecords

    try:
        with db.session() as s, GrpcRecords(settings.mkt_data_grpc) as up, GrpcCalendars(settings.calendar_svc_grpc) as cals:
            return swap_check.run(s, up, cals)
    except CalendarUnavailable as e:
        raise HTTPException(502, str(e)) from None


@router.post("/figi", dependencies=[Depends(require_token)])
def run_figi() -> dict:
    """Ask OpenFIGI about Treasury CUSIPs not looked up yet; keep FIGI, composite FIGI and ticker. 502 if unreachable."""
    from app import figi, figi_job

    try:
        with db.session() as s:
            return figi_job.run(s, settings.openfigi_api_key)
    except figi.FigiError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/futures-figi", dependencies=[Depends(require_token)])
def run_futures_figi() -> dict:
    """Ask OpenFIGI about listed futures contracts: FIGI, Bloomberg ticker, and whether each root is Bloomberg's.
    502 if OpenFIGI can't be reached."""
    from app import figi, futures_figi

    try:
        with db.session() as s:
            return futures_figi.run(s, settings.openfigi_api_key)
    except figi.FigiError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/futures-roots", dependencies=[Depends(require_token)])
def run_futures_roots(body: dict | None = None) -> dict:
    """Search OpenFIGI for the futures candidates' Bloomberg roots (seeds/futures_candidates.toml); stores nothing.
    Body: {"only": ["RP", ...]} to search some. 502 if OpenFIGI can't be reached."""
    from app import figi, futures_roots

    only = (body or {}).get("only") or None
    try:
        return futures_roots.run(settings.openfigi_api_key, only=only)
    except figi.FigiError as e:
        raise HTTPException(502, str(e)) from None
    except futures_roots.CandidateError as e:
        raise HTTPException(500, str(e)) from None


@router.get("/instruments", dependencies=[Depends(require_read_token)])
def instruments(type: str = "", curve: str = "", include_inactive: bool = False) -> dict:
    with db.session() as s:
        return {"instruments": securities.list_instruments(s, type, curve, include_inactive)}


@router.get("/instruments/{key}", dependencies=[Depends(require_read_token)])
def instrument(key: str, as_of: date | None = None) -> dict:
    """By sec_id (digits), short name or alias; an on-the-run name (UST-10Y-OTR) as of a date (default today)."""
    try:
        with db.session() as s:
            if key.isdigit():
                return securities.get(s, sec_id=int(key))
            return securities.get(s, name=key, as_of=as_of)
    except securities.UnknownInstrument as e:
        raise HTTPException(404, str(e)) from None


@router.get("/on-the-run", dependencies=[Depends(require_read_token)])
def on_the_run(as_of: date | None = None) -> dict:
    """Every on-the-run alias on a date (default today) and the security it names."""
    on = as_of or securities.today_ny()
    with db.session() as s:
        return {"as_of": on.isoformat(), "on_the_run": securities.on_the_run(s, on)}


@router.get("/reference-cpi", dependencies=[Depends(require_read_token)])
def reference_cpi(on: date | None = None) -> dict:
    """Treasury's TIPS reference CPI on a date (default today), and whether it used a fallback month."""
    day = on or securities.today_ny()
    with db.session() as s:
        got = securities.reference_cpi(s, day)
    if got is None:
        raise HTTPException(404, f"no reference CPI for {day}: its CPI months aren't loaded")
    return got


@router.get("/resolve", dependencies=[Depends(require_read_token)])
def resolve(scheme: str, value: Annotated[list[str], Query()], as_of: date | None = None) -> dict:
    with db.session() as s:
        return securities.resolve(s, scheme, value, as_of)


@router.get("/search", dependencies=[Depends(require_read_token)])
def search(q: str, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> dict:
    with db.session() as s:
        return {"instruments": securities.search(s, q, limit)}
