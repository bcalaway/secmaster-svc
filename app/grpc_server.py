"""gRPC server (ADR-0020): secmaster-svc's service-to-service API.

Internal-only: listens on GRPC_PORT (9090 by convention), plaintext, reachable
only by other containers on the `home-platform` Docker network at
`secmaster-svc:9090`. Never routed through Traefik. Runs in the same process
and event loop as the FastAPI app (started from its lifespan in app/main.py).

Serves `secmaster_svc.Securities` (proto/securities.proto): instrument
lookups and the batch source-key resolve quote-svc uses. Also the standard
grpc.health.v1.Health service.
"""

import asyncio
from datetime import date

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from app import db, securities
from app.grpc_gen import securities_pb2, securities_pb2_grpc

SECURITIES = securities_pb2.DESCRIPTOR.services_by_name["Securities"].full_name


def _instrument(d: dict) -> securities_pb2.Instrument:
    return securities_pb2.Instrument(
        sec_id=d["sec_id"], short_name=d["short_name"], aliases=d["aliases"], type=d["type"],
        currency=d["currency"], country=d["country"], curve=d["curve"], tenor=d["tenor"],
        calendar=d["calendar"], status=d["status"], description=d["description"],
        identifiers=[securities_pb2.InstrumentIdentifier(**i) for i in d.get("identifiers", [])],
        notes=[securities_pb2.InstrumentNote(**n) for n in d.get("notes", [])],
    )


def _get(sec_id: int, name: str) -> securities_pb2.Instrument:
    with db.session() as s:
        return _instrument(securities.get(s, sec_id=sec_id, name=name))


def _list(type_: str, curve: str, inactive: bool) -> securities_pb2.ListInstrumentsResponse:
    with db.session() as s:
        rows = securities.list_instruments(s, type_, curve, inactive)
    return securities_pb2.ListInstrumentsResponse(instruments=[_instrument(r) for r in rows])


def _resolve(scheme: str, values: list[str], as_of: date | None) -> securities_pb2.ResolveResponse:
    with db.session() as s:
        r = securities.resolve(s, scheme, values, as_of)
    return securities_pb2.ResolveResponse(
        scheme=r["scheme"], matches=[securities_pb2.Match(**m) for m in r["matches"]], unknown=r["unknown"])


def _search(query: str, limit: int) -> securities_pb2.ListInstrumentsResponse:
    with db.session() as s:
        rows = securities.search(s, query, limit or 20)
    return securities_pb2.ListInstrumentsResponse(instruments=[_instrument(r) for r in rows])


def _strings(d: dict | None) -> dict[str, str]:
    """A dict as proto map<string, string>: None as "", booleans as true/false."""
    out = {}
    for k, v in (d or {}).items():
        if v is None:
            out[k] = ""
        elif isinstance(v, bool):
            out[k] = "true" if v else "false"
        else:
            out[k] = str(v)
    return out


def _date(text: str) -> date | None:
    return date.fromisoformat(text) if text else None


def _list_securities(r) -> securities_pb2.ListSecuritiesResponse:
    with db.session() as s:
        got = securities.list_securities(s, r.security_type, r.include_inactive, _date(r.maturing_from),
                                         _date(r.maturing_to), _date(r.as_of), r.limit)
    return securities_pb2.ListSecuritiesResponse(
        as_of=got["as_of"], total=got["total"],
        securities=[securities_pb2.SecuritySummary(**x) for x in got["securities"]])


def _get_security(sec_id: int, name: str, as_of: date | None) -> securities_pb2.Security:
    with db.session() as s:
        d = securities.security(s, sec_id=sec_id, name=name, as_of=as_of)
    return securities_pb2.Security(
        instrument=_instrument(d), terms=_strings(d.get("terms")), provenance=_strings(d.get("provenance")),
        checks=[str(c) for c in d.get("checks") or []],
        auctions=[securities_pb2.Auction(fields=_strings(a)) for a in d.get("auctions", [])],
        on_the_run=[securities_pb2.OnTheRun(**o) for o in d["on_the_run"]],
        index_ratio=_strings(d.get("index_ratio")), strip=_strings(d.get("strip")),
    )


def _list_auctions(start: date, end: date, limit: int) -> securities_pb2.ListAuctionsResponse:
    with db.session() as s:
        got = securities.list_auctions(s, start, end, limit or 500)
    return securities_pb2.ListAuctionsResponse(start=got["start"], end=got["end"], auctions=[
        securities_pb2.AuctionRow(sec_id=a["sec_id"], short_name=a["short_name"],
                                  fields={k: v for k, v in a.items() if k not in ("sec_id", "short_name")})
        for a in got["auctions"]])


class Securities(securities_pb2_grpc.SecuritiesServicer):
    # The database work is synchronous SQLAlchemy, so it runs in a thread.
    async def GetInstrument(self, request, context):
        try:
            return await asyncio.to_thread(_get, request.sec_id, request.name)
        except securities.UnknownInstrument as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    async def ListInstruments(self, request, context):
        return await asyncio.to_thread(_list, request.type, request.curve, request.include_inactive)

    async def Resolve(self, request, context):
        try:
            as_of = date.fromisoformat(request.as_of) if request.as_of else None
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"as_of {request.as_of!r} isn't YYYY-MM-DD")
        return await asyncio.to_thread(_resolve, request.scheme, list(request.values), as_of)

    async def Search(self, request, context):
        return await asyncio.to_thread(_search, request.query, request.limit)

    async def ListSecurities(self, request, context):
        try:
            for f in (request.maturing_from, request.maturing_to, request.as_of):
                _date(f)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "dates are YYYY-MM-DD")
        return await asyncio.to_thread(_list_securities, request)

    async def ListAuctions(self, request, context):
        try:
            start, end = date.fromisoformat(request.start), date.fromisoformat(request.end)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "start and end are YYYY-MM-DD")
        return await asyncio.to_thread(_list_auctions, start, end, request.limit)

    async def GetSecurity(self, request, context):
        try:
            as_of = _date(request.as_of)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"as_of {request.as_of!r} isn't YYYY-MM-DD")
        try:
            return await asyncio.to_thread(_get_security, request.sec_id, request.name, as_of)
        except securities.UnknownInstrument as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))


async def start_grpc_server(port: int) -> tuple[grpc.aio.Server, int]:
    """Start the server; returns it and the bound port (port 0 picks a free one)."""
    server = grpc.aio.server()
    securities_pb2_grpc.add_SecuritiesServicer_to_server(Securities(), server)

    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    bound = server.add_insecure_port(f"[::]:{port}")
    await server.start()
    # "" is the overall server status; each service also reports its own.
    for service in ("", SECURITIES):
        await health_servicer.set(service, health_pb2.HealthCheckResponse.SERVING)
    return server, bound
