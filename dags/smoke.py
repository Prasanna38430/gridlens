"""Smallest dag that proves the scheduler, dag processor and database agree."""

from __future__ import annotations

import pendulum
from airflow.sdk import dag, task


@dag(
    dag_id="smoke",
    schedule=None,
    start_date=pendulum.datetime(2026, 9, 1, tz="Europe/Paris"),
    catchup=False,
    tags=["gridlens"],
)
def smoke():
    @task
    def check_imports() -> str:
        # the point of the dag: prove the bind mounted package is importable
        # inside the container, not just that airflow is running.
        from gridlens.contracts.reference import BiddingZone

        return BiddingZone.FR.value

    check_imports()


smoke()
