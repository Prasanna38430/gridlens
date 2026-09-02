"""Run the bronze expectation suite every morning after the ingest lands.

This is the check that would have caught the two mornings the schedule was
broken in August. The suite has a freshness expectation whose comment says two
days of silence is a broken schedule, and until now nothing ran it unless
somebody typed the command.

Airflow does not own the ingest. EventBridge still fires that at 06:30 and has
done so reliably since the fix. This runs afterwards and tells us whether it
worked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum
from airflow.exceptions import AirflowException
from airflow.sdk import dag, task

# The ingest fires at 06:30 Paris and is allowed two retries an event age of an
# hour apart, so a firing can still be working at 07:00. 07:30 leaves room
# without pushing the signal into the middle of the morning.
SCHEDULE = "30 7 * * *"


@dag(
    dag_id="bronze_quality",
    schedule=SCHEDULE,
    start_date=pendulum.datetime(2026, 9, 1, tz="Europe/Paris"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["gridlens", "quality"],
    doc_md=__doc__,
)
def bronze_quality():
    @task
    def validate() -> dict[str, Any]:
        """Fail the task when any expectation fails, and say which."""
        import boto3

        from gridlens.quality.bronze import run

        result = run(boto3.client("athena", region_name="eu-west-3"))

        summary = (
            f"{result['expectations']} expectations, "
            f"{len(result['failed'])} failed, "
            f"{result['scanned_bytes']} bytes scanned"
        )
        print(summary)

        if not result["passed"]:
            for failure in result["failed"]:
                print(f"  {failure}")
            raise AirflowException(summary)

        return {
            "expectations": result["expectations"],
            "scanned_bytes": result["scanned_bytes"],
        }

    validate()


bronze_quality()
