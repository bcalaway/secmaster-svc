"""secmaster-svc: load Treasury securities from mkt-data's near-raw records (mkt-data's docs/phase-3.md, step 3).

`secmaster_svc__load` runs whenever mkt-data marks the Asset
`mkt_data_treasury_securities` (its capture DAG, after TreasuryDirect's
records change), and nightly as a catch-up. The work runs in the
secmaster-svc container (POST /jobs/load), which re-reads only the months
whose newest capture moved, then asks OpenFIGI about any new CUSIPs (POST
/jobs/figi). `secmaster_svc__rebuild` (manual) re-reads every
month, after a change to how records are typed. Both only call the job API
(ADR-0031 in nyc_pa_aws_gitops); a failed run retries, and Grafana's
"Airflow task failed" alert fires if retries run out.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import Asset, AssetOrTimeSchedule, CronTriggerTimetable, dag, task

# The platform's helper lives at Airflow's DAG root (home_platform_jobs.py);
# this repo's dags/ is delivered to dags/secmaster-svc/ there, so the root is
# one level up. Airflow normally has it on sys.path; this makes sure of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job

# Marked by mkt-data's capture and rebuild DAGs when TreasuryDirect's records change.
TREASURY_SECURITIES = Asset("mkt_data_treasury_securities")


def report(result: dict) -> dict:
    """One log line per notable check, so a long answer stays readable in the task log."""
    for item in result.get("notable_checks", []):
        print(f"check: {item.get('short_name')}: {'; '.join(item.get('checks', []))}")
    cpi = result.get("tips_cpi") or {}
    for m in cpi.get("mismatches", []):
        print(f"tips cpi: {m.get('auction')}: {m.get('what')}: ours {m.get('ours')}, TreasuryDirect {m.get('treasurydirect')}")
    out = {k: v for k, v in result.items() if k != "notable_checks"}
    if cpi:
        out["tips_cpi"] = {k: v for k, v in cpi.items() if k != "mismatches"}
    return out


@dag(
    dag_id="secmaster_svc__load",
    schedule=AssetOrTimeSchedule(
        timetable=CronTriggerTimetable("27 7 * * *", timezone="UTC"),  # nightly catch-up, 07:27 UTC
        assets=[TREASURY_SECURITIES],
    ),
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),
    default_args={"retries": 3, "retry_delay": timedelta(minutes=10)},
    tags=["secmaster-svc", "treasury", "securities"],
    doc_md=__doc__,
)
def load():
    @task
    def run() -> dict:
        return report(call_app_job("secmaster-svc", "load", timeout=3600))

    @task(retries=2, retry_delay=timedelta(minutes=15))
    def map_figis() -> dict:
        """New CUSIPs to OpenFIGI (FIGI, composite FIGI, ticker); a failure here never holds up the load."""
        return call_app_job("secmaster-svc", "figi", timeout=1800)

    run() >> map_figis()


@dag(
    dag_id="secmaster_svc__rebuild",
    schedule=None,
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    tags=["secmaster-svc", "treasury", "securities"],
    doc_md=__doc__,
)
def rebuild():
    @task
    def run() -> dict:
        return report(call_app_job("secmaster-svc", "rebuild", timeout=3600))

    run()


load()
rebuild()
