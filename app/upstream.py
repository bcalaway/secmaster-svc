"""What secmaster-svc reads from: mkt-data's near-raw records and observations (gRPC).

`Upstream` is the interface the load job uses; `GrpcRecords` talks to mkt-data
on the home-platform network (proto/records.proto and proto/observations.proto, copied
from mkt-data): TreasuryDirect's and the MSPD's records, and BLS's CPI-U.
Tests use a fake with the same methods.
"""

import json
from dataclasses import dataclass
from typing import Protocol

TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Period:
    period: str  # YYYY-MM
    latest_capture_id: int
    records: int


@dataclass(frozen=True)
class Rec:
    record_id: int
    record_type: str  # auction
    source_key: str  # CUSIP/issue date
    as_of: str  # YYYY-MM-DD
    fields: dict  # as published
    capture_id: int


@dataclass(frozen=True)
class Obs:
    observation_id: int
    source_key: str  # CUUR0000SA0
    as_of: str  # YYYY-MM-DD
    field: str  # index
    value: str  # decimal string, as printed
    capture_id: int


class Upstream(Protocol):
    def list_periods(self, source: str) -> list[Period]: ...
    def get_period(self, source: str, period: str) -> list[Rec]: ...
    def list_obs_periods(self, source: str) -> list[Period]: ...
    def get_obs_period(self, source: str, period: str) -> list[Obs]: ...


class GrpcRecords:
    """mkt-data's Records service over gRPC. Use as a context manager."""

    def __init__(self, target: str):
        self.target = target

    def __enter__(self):
        import grpc  # here, so the rest of the app (and its tests) runs without compiled grpcio

        from app.grpc_gen import observations_pb2_grpc, records_pb2_grpc

        self._channel = grpc.insecure_channel(self.target)
        self._stub = records_pb2_grpc.RecordsStub(self._channel)
        self._obs = observations_pb2_grpc.ObservationsStub(self._channel)
        return self

    def __exit__(self, *exc):
        self._channel.close()

    def list_periods(self, source: str) -> list[Period]:
        from app.grpc_gen import records_pb2 as pb

        r = self._stub.ListPeriods(pb.ListRecordPeriodsRequest(source=source), timeout=TIMEOUT_SECONDS)
        return [Period(p.period, p.latest_capture_id, p.records) for p in r.periods]

    def get_period(self, source: str, period: str) -> list[Rec]:
        from app.grpc_gen import records_pb2 as pb

        r = self._stub.GetPeriod(pb.GetRecordPeriodRequest(source=source, period=period), timeout=TIMEOUT_SECONDS)
        return [Rec(x.id, x.record_type, x.source_key, x.as_of, json.loads(x.fields_json), x.capture_id)
                for x in r.records]

    def list_obs_periods(self, source: str) -> list[Period]:
        from app.grpc_gen import observations_pb2 as pb

        r = self._obs.ListPeriods(pb.ListPeriodsRequest(source=source), timeout=TIMEOUT_SECONDS)
        return [Period(p.period, p.latest_capture_id, p.values) for p in r.periods]

    def get_obs_period(self, source: str, period: str) -> list[Obs]:
        from app.grpc_gen import observations_pb2 as pb

        r = self._obs.GetPeriod(pb.GetPeriodRequest(source=source, period=period), timeout=TIMEOUT_SECONDS)
        return [Obs(v.id, v.source_key, v.as_of, v.field, v.value, v.capture_id) for v in r.values]
