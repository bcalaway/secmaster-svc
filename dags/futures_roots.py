"""secmaster-svc: search OpenFIGI for Bloomberg roots of the futures to add (mkt-data's docs/phase-4.md, step 2c).

Manual only. The work runs in the secmaster-svc container (POST /jobs/futures-roots): it searches
OpenFIGI for each product in seeds/futures_candidates.toml and logs, per product, the futures found
grouped by root and name, so a person can read off each product's root before it goes into
seeds/futures.toml. Stores nothing, so safe to re-run. The run form's `only` takes CME codes
(comma-separated) to search a few. This DAG only calls the job (ADR-0031 in nyc_pa_aws_gitops).
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task

# The platform's helper lives at Airflow's DAG root (home_platform_jobs.py);
# this repo's dags/ is delivered to dags/secmaster-svc/ there, so the root is
# one level up. Airflow normally has it on sys.path; this makes sure of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job


def report(result: dict) -> dict:
    """One line per candidate and query, then one per group of futures found."""
    for code, c in sorted((result.get("candidates") or {}).items()):
        print(f"roots: {code} ({c.get('name')}, {c.get('sector')}, {c.get('exch_code')}): "
              f"searched {'; '.join(c.get('queries', []))}")
        groups = c.get("groups") or []
        if not groups:
            print(f"roots: {code}:   nothing on {c.get('exch_code')}")
        for g in groups:
            latest = "-".join(str(x) for x in g.get("latest") or []) or "?"
            print(f"roots: {code}:   {g.get('root')} = {g.get('name')}, {g.get('contracts')} contracts, "
                  f"latest {latest}: {g.get('example')}")
        if c.get("more_groups"):
            print(f"roots: {code}:   and {c['more_groups']} more groups")
    return {k: v for k, v in result.items() if k != "candidates"}


@dag(
    dag_id="secmaster_svc__futures_roots",
    schedule=None,
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    params={"only": Param("", type="string", description="CME codes to search, comma-separated; empty for all")},
    tags=["secmaster-svc", "futures"],
    doc_md=__doc__,
)
def futures_roots():
    @task
    def search() -> dict:
        p = get_current_context()["params"]
        only = [c.strip() for c in str(p.get("only") or "").split(",") if c.strip()]
        return report(call_app_job("secmaster-svc", "futures-roots", {"only": only}, timeout=1500))

    search()


futures_roots()
