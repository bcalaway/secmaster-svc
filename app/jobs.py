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


@router.get("/instruments", dependencies=[Depends(require_read_token)])
def instruments(type: str = "", curve: str = "", include_inactive: bool = False) -> dict:
    with db.session() as s:
        return {"instruments": securities.list_instruments(s, type, curve, include_inactive)}


@router.get("/instruments/{key}", dependencies=[Depends(require_read_token)])
def instrument(key: str) -> dict:
    """By sec_id (digits) or by short name or alias."""
    try:
        with db.session() as s:
            if key.isdigit():
                return securities.get(s, sec_id=int(key))
            return securities.get(s, name=key)
    except securities.UnknownInstrument as e:
        raise HTTPException(404, str(e)) from None


@router.get("/resolve", dependencies=[Depends(require_read_token)])
def resolve(scheme: str, value: Annotated[list[str], Query()], as_of: date | None = None) -> dict:
    with db.session() as s:
        return securities.resolve(s, scheme, value, as_of)


@router.get("/search", dependencies=[Depends(require_read_token)])
def search(q: str, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> dict:
    with db.session() as s:
        return {"instruments": securities.search(s, q, limit)}
