"""Rebuild last month as production saw it, and fail if it comes out different.

Gold is only reproducible if rebuilding it from bronze, bounded at what
production had learned when it last ran, lands on exactly the same rows. This
rebuilds the previous calendar month into a throwaway schema, compares every
table with production row for row, and drops the schema whatever happened.

The first run of this, by hand, failed: 299 of 364 August rows differed between
two builds of identical input, because daily energy was a floating point sum.

Production gold is whatever `make dbt` last built, and the bound comes from the
run manifest that build wrote. If production was built by older code than the
dags folder holds, a failure here can be the code changing rather than the data
being irreproducible. The manifest's git sha is printed to tell which.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pendulum
from airflow.exceptions import AirflowException
from airflow.sdk import dag, task

REGION = "eu-west-3"
PARIS = ZoneInfo("Europe/Paris")

# dbt has its own virtualenv in the image, see docker/airflow.Dockerfile
DBT = "/opt/dbt/bin/dbt"
PROJECT_DIR = "/opt/airflow/project/transform"
TEARDOWN = "/opt/airflow/project/scripts/dbt_teardown.py"
# inside the container, never in the project the host's dbt also uses
DBT_ARTIFACTS = "/tmp/dbt-audit"


def _run_day(context: dict[str, Any]) -> date:
    # run_after rather than logical_date, which is null on a manual run. It is
    # the same across retries, so every task in a run agrees on the schema.
    run_after = context["dag_run"].run_after
    day: date = run_after.astimezone(PARIS).date()
    return day


def _inputs(planned: dict[str, Any], context: dict[str, Any]) -> tuple[Any, Any]:
    """The window and the production run, the same in every task of a run."""
    from datetime import datetime

    from gridlens.quality import restatement

    # the run is taken from plan rather than read again, so a `make dbt`
    # landing halfway through cannot change what the later tasks compare with
    run = restatement.ProductionRun(
        git_sha=planned["git_sha"],
        snapshot_id=int(planned["snapshot_id"]),
        learned_up_to=datetime.fromisoformat(planned["learned_up_to"]),
    )
    return restatement.last_month(_run_day(context)), run


@dag(
    dag_id="restatement_audit",
    # clear of the 07:30 and 08:00 dags, so dbt never shares the scheduler
    # container's memory with the quality suite. Morning rather than the middle
    # of the night because the stack runs on a laptop, and a dag only runs
    # while the laptop is on.
    schedule="0 9 * * *",
    start_date=pendulum.datetime(2026, 10, 1, tz="Europe/Paris"),
    catchup=False,
    max_active_runs=1,
    tags=["gridlens", "quality"],
    doc_md=__doc__,
)
def restatement_audit():
    @task
    def plan(**context: Any) -> dict[str, Any]:
        import boto3

        from gridlens.quality import restatement

        day = _run_day(context)
        window = restatement.last_month(day)
        run = restatement.production_run(boto3.client("athena", region_name=REGION))
        print(f"auditing {window.first_day} to {window.last_day}")
        print(
            f"production built by {run.git_sha}, read snapshot {run.snapshot_id}, "
            f"learned up to {run.learned_up_to}"
        )
        return {
            "schema": restatement.schema_for(day),
            "git_sha": run.git_sha,
            # a string, because it is past 2**53 and anything on the way that
            # reads json numbers as doubles would round it to another snapshot
            "snapshot_id": str(run.snapshot_id),
            "learned_up_to": run.learned_up_to.isoformat(),
        }

    @task
    def check_clocks(planned: dict[str, Any], **context: Any) -> dict[str, int]:
        import boto3

        from gridlens.quality import restatement

        window, run = _inputs(planned, context)
        counts = restatement.check_clocks(
            boto3.client("athena", region_name=REGION), window, run
        )
        print(f"bronze rows in the window, by snapshot and by known_at: {counts}")
        return counts

    @task
    def restate(planned: dict[str, Any], **context: Any) -> None:
        from gridlens.quality import restatement

        window, run = _inputs(planned, context)
        output = restatement.restate(
            DBT,
            PROJECT_DIR,
            planned["schema"],
            window,
            run,
            artifacts_dir=DBT_ARTIFACTS,
        )
        print(output)

    @task
    def compare(planned: dict[str, Any], **context: Any) -> dict[str, dict[str, int]]:
        import boto3

        from gridlens.quality import restatement

        window, _ = _inputs(planned, context)
        results = restatement.compare(
            boto3.client("athena", region_name=REGION), planned["schema"], window
        )
        for table, counts in results.items():
            print(f"{table}: {counts}")

        failed = restatement.differences(results)
        if failed:
            raise AirflowException(
                f"restated {', '.join(failed)} differ from production built by "
                f"{planned['git_sha']}"
            )
        return results

    # all_done, so a failed build or a failed comparison still drops the
    # schema. Named from the run date rather than taken from plan, so it runs
    # even when plan is what failed.
    @task(trigger_rule="all_done")
    def teardown(**context: Any) -> None:
        import subprocess
        import sys

        import boto3

        from gridlens.quality import restatement

        schema = restatement.schema_for(_run_day(context))
        account = boto3.client("sts", region_name=REGION).get_caller_identity()[
            "Account"
        ]
        subprocess.run(
            [
                sys.executable,
                TEARDOWN,
                schema,
                "--data-prefix",
                f"s3://gridlens-lake-{account}/audit/",
            ],
            check=True,
        )

    # teardown always succeeds when it can, and airflow judges a run by its
    # leaves, so without this the run would go green on a failed comparison
    @task
    def verdict(results: dict[str, dict[str, int]]) -> None:
        rows = sum(counts["restated"] for counts in results.values())
        print(f"{rows} restated rows across {len(results)} tables, all identical")

    planned = plan()
    checked = check_clocks(planned)
    built = restate(planned)
    compared = compare(planned)
    dropped = teardown()

    checked >> built >> compared >> dropped
    dropped >> verdict(compared)


restatement_audit()
