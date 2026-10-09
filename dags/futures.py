"""secmaster-svc: generate futures products and contracts, then their FIGIs (mkt-data's docs/phase-4.md, steps 2a, 2b).

`secmaster_svc__futures` runs daily just after midnight New York, when a contract's status (listed,
delivery, expired) and the generics (`TY1`, `TY2`) move to the new day, and can be triggered by hand
after a change to seeds/futures.toml or to a calendar. The work runs in the secmaster-svc container
(POST /jobs/futures): it reads the seed, fetches every calendar the rules count in from calendar-svc
and brings products, contracts, CME codes and generics in line. Then the container asks OpenFIGI
about listed contracts not yet confirmed (POST /jobs/futures-figi, step 2b): FIGIs, Bloomberg's
live tickers and whether each product's root is Bloomberg's. This DAG only calls the two jobs
(ADR-0031 in nyc_pa_aws_gitops). Idempotent; a failed run retries, and Grafana's "Airflow task failed" alert fires
if retries run out.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import CronTriggerTimetable, dag, task

# The platform's helper lives at Airflow's DAG root (home_platform_jobs.py);
# this repo's dags/ is delivered to dags/secmaster-svc/ there, so the root is
# one level up. Airflow normally has it on sys.path; this makes sure of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job


def report(result: dict) -> dict:
    """One log line per product (contracts, listed, front), then the rest of the summary."""
    for root, p in sorted((result.get("products") or {}).items()):
        print(f"futures: {root}: {p.get('contracts')} contracts, {p.get('listed')} listed, front {p.get('front')}")
    return {k: v for k, v in result.items() if k != "products"}


def report_figi(result: dict) -> dict:
    """One log line per product (how OpenFIGI's answers confirm its root), then errors and conflicts."""
    for root, p in sorted((result.get("products") or {}).items()):
        line = (f"futures figi: {root}: {p.get('confirmed')} of {p.get('listed')} confirmed {p.get('via')}, "
                f"{p.get('mismatch')} mismatch, {p.get('not_found')} not found, {p.get('error')} error")
        if p.get("bloomberg_roots"):
            line += f"; Bloomberg's root: {', '.join(p['bloomberg_roots'])}"
        if p.get("example"):
            ex = p["example"]
            line += (f"; e.g. {ex.get('contract')} = {ex.get('ticker')} ({ex.get('name')}, {ex.get('exch_code')}, "
                     f"{ex.get('figi')})")
        if p.get("exchange_said"):
            line += f"; by CME's code OpenFIGI said {p['exchange_said']}"
        if p.get("first_not_found"):
            m = p["first_not_found"]
            line += f"; first not found {m.get('contract')}: {'; '.join(m.get('said', []))}"
        print(line)
    for e in result.get("errors", []):
        print(f"futures figi error: {e.get('contract')}: {e.get('by_ticker')} / {e.get('by_exchange')}")
    for r in result.get("retired", []):
        print(f"futures figi retired: {r.get('contract')}: {r.get('ticker')} ({r.get('figi')}), now {r.get('now')}")
    for c in result.get("conflicts", []):
        print(f"futures figi conflict: {c.get('contract')}: ticker {c.get('ticker')} is another instrument's")
    return {k: v for k, v in result.items() if k != "products"}


@dag(
    dag_id="secmaster_svc__futures",
    schedule=CronTriggerTimetable("12 0 * * *", timezone="America/New_York"),
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=20),
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    tags=["secmaster-svc", "futures"],
    doc_md=__doc__,
)
def futures():
    @task
    def generate() -> dict:
        return report(call_app_job("secmaster-svc", "futures", timeout=900))

    @task(retries=2, retry_delay=timedelta(minutes=15))
    def map_figis() -> dict:
        """Listed contracts to OpenFIGI (FIGI, Bloomberg ticker, root check); a failure here never holds up generation."""
        return report_figi(call_app_job("secmaster-svc", "futures-figi", timeout=900))

    generate() >> map_figis()


futures()
