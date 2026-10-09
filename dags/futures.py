"""secmaster-svc: generate futures products and contracts (mkt-data's docs/phase-4.md, step 2a).

`secmaster_svc__futures` runs daily just after midnight New York, when a contract's status (listed,
delivery, expired) and the generics (`TY1`, `TY2`) move to the new day, and can be triggered by hand
after a change to seeds/futures.toml or to a calendar. The work runs in the secmaster-svc container
(POST /jobs/futures): it reads the seed, fetches every calendar the rules count in from calendar-svc
and brings products, contracts, CME codes and generics in line. This DAG only calls it (ADR-0031 in
nyc_pa_aws_gitops). Idempotent; a failed run retries, and Grafana's "Airflow task failed" alert fires
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

    generate()


futures()
