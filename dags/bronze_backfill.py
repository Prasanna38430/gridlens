"""Fill settlement days bronze is missing, without being asked.

Three gaps have been filled by hand in the last fortnight: two when the
scheduler payload broke, and three more when ENTSO-E returned 5xx for three
mornings running. Each time the sequence was identical, so it belongs in a dag
rather than in my shell history.

This does not own the ingest. EventBridge still fires that at 06:30. This runs
afterwards, notices what never arrived, and asks the backfill lambda for it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum
from airflow.exceptions import AirflowSkipException
from airflow.sdk import dag, task

# A week is enough to cover an outage and short enough that a genuinely empty
# table cannot trigger a month of refetching. Older holes are a deliberate
# backfill, run by hand, not something a scheduler should decide to do.
LOOKBACK_DAYS = 7

REGION = "eu-west-3"
BACKFILL_FUNCTION = "gridlens-backfill-entsoe"


@dag(
    dag_id="bronze_backfill",
    # after bronze_quality at 07:30, so a failing freshness check and a repair
    # attempt do not race each other for the same answer.
    schedule="0 8 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz="Europe/Paris"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=15)},
    tags=["gridlens", "ingest"],
    doc_md=__doc__,
)
def bronze_backfill():
    @task
    def find_gaps(**context: Any) -> list[str]:
        import boto3

        from gridlens.lake.gaps import missing_settlement_days

        # yesterday is the newest day the 06:30 run could have delivered
        today = context["dag_run"].run_after.date()
        end = today - timedelta(days=1)
        start = end - timedelta(days=LOOKBACK_DAYS - 1)

        gaps = missing_settlement_days(
            boto3.client("athena", region_name=REGION), start=start, end=end
        )
        print(f"window {start} to {end}, missing {len(gaps)}: {gaps}")
        return [day.isoformat() for day in gaps]

    @task
    def backfill(gaps: list[str], **context: Any) -> dict[str, Any]:
        import json

        import boto3
        from botocore.config import Config

        if not gaps:
            raise AirflowSkipException("nothing missing")

        # known_at is the run's own timestamp rather than a clock reading, so a
        # retry of this task merges onto the rows the first attempt wrote
        # instead of inserting the range again as a fabricated revision. Same
        # reasoning as the scheduled time eventbridge substitutes for the daily
        # run, and the reason that outage did not corrupt anything.
        known_at = context["dag_run"].run_after.replace(microsecond=0)

        payload = {
            "start_date": min(gaps),
            "end_date": max(gaps),
            "known_at": known_at.isoformat(),
        }
        print(f"invoking {BACKFILL_FUNCTION} with {payload}")

        # the default client read timeout is 60 seconds against a function
        # allowed 900, so the client gives up while the function keeps running
        client = boto3.client(
            "lambda",
            region_name=REGION,
            config=Config(read_timeout=900, retries={"max_attempts": 0}),
        )
        response = client.invoke(
            FunctionName=BACKFILL_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        body = json.loads(response["Payload"].read())

        if response.get("FunctionError"):
            raise RuntimeError(f"backfill failed: {body}")

        print(f"backfill returned {body}")
        result: dict[str, Any] = body
        return result

    backfill(find_gaps())


bronze_backfill()
