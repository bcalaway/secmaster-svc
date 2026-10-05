import asyncio
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from app.grpc_server import SECURITIES, start_grpc_server

SEEDS = Path(__file__).resolve().parent.parent / "seeds"


async def _call(fn):
    # Port 0: the OS picks a free port, so tests never collide with 9090.
    server, port = await start_grpc_server(0)
    try:
        async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
            return await fn(channel)
    finally:
        await server.stop(grace=None)


def test_health_reports_serving():
    async def check(channel):
        stub = health_pb2_grpc.HealthStub(channel)
        overall = await stub.Check(health_pb2.HealthCheckRequest(service=""))
        sec = await stub.Check(health_pb2.HealthCheckRequest(service=SECURITIES))
        return overall.status, sec.status

    serving = health_pb2.HealthCheckResponse.SERVING
    assert asyncio.run(_call(check)) == (serving, serving)


def test_securities(migrated_db):
    from app import db, seed
    from app.grpc_gen import securities_pb2 as pb
    from app.grpc_gen import securities_pb2_grpc

    with db.session() as s:
        seed.apply_all(s, SEEDS)

    async def read(channel):
        stub = securities_pb2_grpc.SecuritiesStub(channel)
        ten = await stub.GetInstrument(pb.GetInstrumentRequest(name="ust-10y-cmt"))
        curve = await stub.ListInstruments(pb.ListInstrumentsRequest(type="cmt_yield", curve="UST"))
        resolved = await stub.Resolve(pb.ResolveRequest(scheme="UST-PAR", values=["BC_10YEAR", "NOPE"], as_of="2026-10-01"))
        found = await stub.Search(pb.SearchRequest(query="6W"))
        errors = []
        for call in (stub.GetInstrument(pb.GetInstrumentRequest(name="NOPE")),
                     stub.Resolve(pb.ResolveRequest(scheme="UST-PAR", values=["X"], as_of="soon"))):
            try:
                await call
                errors.append(None)
            except grpc.aio.AioRpcError as e:
                errors.append(e.code())
        return ten, curve, resolved, found, errors

    ten, curve, resolved, found, errors = asyncio.run(_call(read))
    assert ten.short_name == "UST-10Y-CMT" and len(ten.identifiers) == 3 and ten.notes[0].key == "par-curve-method-2021"
    assert len(curve.instruments) == 14 and curve.instruments[0].short_name == "UST-1M-CMT"
    assert [m.short_name for m in resolved.matches] == ["UST-10Y-CMT"] and list(resolved.unknown) == ["NOPE"]
    assert [i.short_name for i in found.instruments] == ["UST-1.5M-CMT"]
    assert errors == [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT]
