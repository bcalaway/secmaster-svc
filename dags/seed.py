"""secmaster-svc: apply the seed files (seeds/*.toml) now.

Manual only (no schedule): every container start applies them already, so a
deploy that changes a seed file takes effect at once. This is for re-applying
by hand, for example after fixing a conflict the start reported. The work
runs in the secmaster-svc container (POST /jobs/seed); this DAG only calls it
(ADR-0031 in nyc_pa_aws_gitops). Safe to re-run: the seed is idempotent.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import dag, task

# The platform's helper lives at Airflow's DAG root (home_platform_jobs.py);
# this repo's dags/ is delivered to dags/secmaster-svc/ there, so the root is
# one level up. Airflow normally has it on sys.path; this makes sure of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job


@dag(
    dag_id="secmaster_svc__seed",
    schedule=None,
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=10),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=2)},
    tags=["secmaster-svc", "seed"],
    doc_md=__doc__,
)
def seed():
    @task
    def apply() -> dict:
        return call_app_job("secmaster-svc", "seed", timeout=120)

    apply()


seed()
