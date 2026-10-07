"""Prometheus metrics (GET /metrics): the security master's contents and its seed runs.

Text format, computed from the database on each scrape. Prometheus scrapes it
on the home-platform network as secmaster-svc:8000 (nyc_pa_aws_gitops's
prometheus.yml); no auth, like the other scrape targets, and nothing in it is
sensitive. Labels use readable names.

Unmapped identifiers (an observation key with no instrument) are counted by
quote-svc, which sees the keys (phase 2, step B5).
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import Identifier, Instrument, InstrumentName, LoadRun, SecurityTerms, SeedRun, UntypedRecord
from app.treasuries import check_code

router = APIRouter()


def _epoch(t: datetime) -> float:
    # Postgres returns aware timestamps; SQLite (tests) returns naive UTC ones.
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def _escape(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            num = int(value) if float(value).is_integer() else value
            self.lines.append(f"{name}{{{body}}} {num}" if body else f"{name} {num}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(s) -> str:
    out = _Out()
    out.metric("secmaster_svc_up", "gauge", "1 when the database answered this scrape.", [({}, 1)])
    inst = s.execute(
        select(Instrument.type, Instrument.curve, Instrument.status, func.count())
        .group_by(Instrument.type, Instrument.curve, Instrument.status)
    ).all()
    out.metric("secmaster_svc_instruments", "gauge", "Instruments, by type, curve and status.",
               [({"type": t, "curve": c or "", "status": st}, n) for t, c, st, n in sorted(inst, key=str)])
    ids = s.execute(
        select(Identifier.scheme, func.count()).where(Identifier.removed_at.is_(None)).group_by(Identifier.scheme)
    ).all()
    out.metric("secmaster_svc_identifiers", "gauge", "Current identifiers, by scheme (UST-PAR, H15-TCM, FRED).",
               [({"scheme": sc}, n) for sc, n in sorted(ids)])
    aliases = s.scalar(select(func.count()).select_from(InstrumentName).where(
        InstrumentName.kind == "alias", InstrumentName.removed_at.is_(None))) or 0
    out.metric("secmaster_svc_aliases", "gauge", "Current aliases (old or alternative names).", [({}, aliases)])
    no_short = s.scalar(select(func.count()).select_from(Instrument).where(
        ~Instrument.sec_id.in_(select(InstrumentName.sec_id).where(
            InstrumentName.kind == "short", InstrumentName.removed_at.is_(None))))) or 0
    out.metric("secmaster_svc_instruments_without_short_name", "gauge",
               "Instruments with no current short name; should be 0.", [({}, no_short)])

    last_ok, latest = {}, {}
    for r in s.scalars(select(SeedRun).order_by(SeedRun.id)):
        latest[r.seed] = r
        if r.outcome == "ok":
            last_ok[r.seed] = r
    out.metric("secmaster_svc_seed_last_success_timestamp_seconds", "gauge",
               "When each seed file was last applied successfully.",
               [({"seed": k}, _epoch(r.finished_at or r.started_at)) for k, r in sorted(last_ok.items())])
    out.metric("secmaster_svc_seed_ok", "gauge", "1 if the latest run of each seed file succeeded, 0 if it failed.",
               [({"seed": k}, int(r.outcome == "ok")) for k, r in sorted(latest.items())])
    changes = []
    for k, r in sorted(last_ok.items()):
        summary = json.loads(r.summary or "{}")
        changes.append(({"seed": k}, int(bool(summary.get("changed")))))
    out.metric("secmaster_svc_seed_last_changed", "gauge",
               "1 if the last successful run of each seed file changed anything.", changes)

    # Treasury securities (phase 3): the load from mkt-data, untyped records, terms that don't add up.
    runs = list(s.scalars(select(LoadRun).order_by(LoadRun.id.desc()).limit(50)))
    ok = next((r for r in runs if r.outcome == "ok"), None)
    out.metric("secmaster_svc_load_last_success_timestamp_seconds", "gauge",
               "When the last successful load from mkt-data finished.", [({}, _epoch(ok.finished_at))] if ok else [])
    out.metric("secmaster_svc_load_ok", "gauge", "1 if the latest load from mkt-data succeeded, 0 if it failed.",
               [({}, int(runs[0].outcome == "ok"))] if runs else [])
    untyped = s.execute(select(UntypedRecord.source, func.count()).group_by(UntypedRecord.source)).all()
    out.metric("secmaster_svc_untyped_records", "gauge",
               "Near-raw records the load couldn't read as a security, by source.",
               [({"source": src}, n) for src, n in sorted(untyped)])
    by_type: dict[str, list[int]] = {}
    for sec_type, checks in s.execute(select(SecurityTerms.security_type, SecurityTerms.checks)
                                      .where(SecurityTerms.superseded_at.is_(None))):
        counts = by_type.setdefault(sec_type, [0, 0])
        counts[0] += 1
        counts[1] += int(bool(checks))
    out.metric("secmaster_svc_securities", "gauge", "Treasury securities with current terms, by security type.",
               [({"security_type": t}, n) for t, (n, _) in sorted(by_type.items())])
    out.metric("secmaster_svc_securities_with_checks", "gauge",
               "Treasury securities whose terms have a check that didn't pass, by security type.",
               [({"security_type": t}, c) for t, (_, c) in sorted(by_type.items())])
    codes: dict[tuple[str, str], int] = {}
    for sec_type, checks in s.execute(select(SecurityTerms.security_type, SecurityTerms.checks)
                                      .where(SecurityTerms.superseded_at.is_(None))):
        for code in {check_code(c) for c in checks or []}:
            codes[(sec_type, code)] = codes.get((sec_type, code), 0) + 1
    out.metric("secmaster_svc_security_checks", "gauge",
               "Treasury securities with each kind of check that didn't pass, by security type and check.",
               [({"security_type": t, "check": c}, n) for (t, c), n in sorted(codes.items())])
    return out.text()


@router.get("/metrics")
def metrics() -> Response:
    try:
        with db.session() as s:
            body = render(s)
    except (db.DatabaseNotConfigured, SQLAlchemyError):
        body = "# HELP secmaster_svc_up 1 when the database answered this scrape.\n# TYPE secmaster_svc_up gauge\nsecmaster_svc_up 0\n"
    return Response(body, media_type="text/plain; version=0.0.4")
