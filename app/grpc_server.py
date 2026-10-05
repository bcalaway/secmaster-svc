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
