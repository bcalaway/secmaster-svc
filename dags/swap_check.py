"""secmaster-svc: check the SPGMI swap curve files' conventions against swap_terms (mkt-data's docs/phase-4.md,
"Swap curves").

`secmaster_svc__swap_check` runs whenever mkt-data marks the Asset `mkt_data_swap_curves` (a swap curve capture
changed par rates) and nightly as a catch-up. The work runs in the secmaster-svc container (POST /jobs/swap-check),
which reads mkt-data's curve records and calendar-svc's ISDA calendars and records the result per source; this DAG
only calls it (ADR-0031 in nyc_pa_aws_gitops). A difference doesn't fail the run: it shows in the log and in the
metric secmaster_svc_swap_conventions_ok.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import Asset, AssetOrTimeSchedule, CronTriggerTimetable, dag, task

# The platform's helper lives at Airflow's DAG root (see load.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job

# Marked by mkt-data's mkt_data__swap_curves_capture and _backfill when a curve's par rates change.
SWAP_CURVES = Asset("mkt_data_swap_curves")


def report(result: dict) -> dict:
    """One log line per source, and one per difference shown."""
    for source, r in (result.get("sources") or {}).items():
        print(f"{source}: {r.get('outcome')}, {r.get('files')} files, last {r.get('last')}, "
              f"{r.get('differences')} differences")
        for d in r.get("first") or []:
            print(f"  {d.get('period')} {d.get('field')}: file {d.get('file')!r}, seed {d.get('seed')!r}")
    return result


@dag(
    dag_id="secmaster_svc__swap_check",
    schedule=AssetOrTimeSchedule(
        timetable=CronTriggerTimetable("41 7 * * *", timezone="UTC"),  # nightly catch-up, 07:41 UTC
        assets=[SWAP_CURVES],
    ),
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    default_args={"retries": 3, "retry_delay": timedelta(minutes=10)},
    tags=["secmaster-svc", "swaps", "isda"],
    doc_md=__doc__,
)
def swap_check():
    @task
    def run() -> dict:
        return report(call_app_job("secmaster-svc", "swap-check", timeout=600))

    run()


swap_check()
