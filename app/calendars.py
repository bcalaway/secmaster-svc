"""calendar-svc's closed days, for generating futures contracts (mkt-data's docs/phase-4.md, step 2a).

`CalendarSource` is what app/futures_load.py needs: each calendar's covered years and its closed
weekdays in a range. `GrpcCalendars` asks calendar-svc on the home-platform network
(proto/calendars.proto, copied from calendar-svc). Each calendar is fetched once per run, whole.
Tests use a fake with the same methods.
"""

from datetime import date
from typing import Protocol

from app.futures import Calendars

TIMEOUT_SECONDS = 60


class CalendarUnavailable(RuntimeError):
    pass


class CalendarSource(Protocol):
    def years(self) -> dict[str, tuple[int, int]]: ...
    def closed(self, calendar: str, start: date, end: date) -> set[date]: ...


def fetch(source: CalendarSource, names, start: date, end: date) -> Calendars:
    """Every named calendar's closed weekdays (early closes are open) over a range, and its covered years."""
    years = source.years()
    missing = [n for n in names if n not in years]
    if missing:
        raise CalendarUnavailable(f"calendar-svc has no calendar {', '.join(sorted(missing))}")
    lo = {n: max(start, date(years[n][0], 1, 1)) for n in names}
    hi = {n: min(end, date(years[n][1], 12, 31)) for n in names}
    closed = {n: frozenset(source.closed(n, lo[n], hi[n])) for n in names}
    return Calendars(closed, {n: years[n] for n in names})


class GrpcCalendars:
    """calendar-svc's Calendars service over gRPC. Use as a context manager."""

    def __init__(self, target: str):
        self.target = target

    def __enter__(self):
        import grpc  # here, so the rest of the app (and its tests) runs without compiled grpcio

        from app.grpc_gen import calendars_pb2_grpc

        self._grpc = grpc
        self._channel = grpc.insecure_channel(self.target)
        self._stub = calendars_pb2_grpc.CalendarsStub(self._channel)
        return self

    def __exit__(self, *exc):
        self._channel.close()

    def years(self) -> dict[str, tuple[int, int]]:
        from app.grpc_gen import calendars_pb2 as pb

        try:
            r = self._stub.ListCalendars(pb.ListCalendarsRequest(), timeout=TIMEOUT_SECONDS)
        except self._grpc.RpcError as e:
            raise CalendarUnavailable(f"calendar-svc ListCalendars: {e.code().name}") from None
        return {c.name: (c.first_year, c.last_year) for c in r.calendars if c.first_year}

    def closed(self, calendar: str, start: date, end: date) -> set[date]:
        from app.grpc_gen import calendars_pb2 as pb

        try:
            r = self._stub.Closes(pb.ClosesRequest(calendar=calendar, start=start.isoformat(), end=end.isoformat()),
                                  timeout=TIMEOUT_SECONDS)
        except self._grpc.RpcError as e:
            raise CalendarUnavailable(f"calendar-svc Closes {calendar}: {e.code().name}") from None
        return {date.fromisoformat(c.date) for c in r.closes if c.status == "closed"}
