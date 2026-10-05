"""Rebuild last month as production saw it, and check it comes out the same.

Gold is only reproducible if rebuilding it from bronze, bounded at what
production had learned when it last ran, lands on exactly the same rows. The
restatement_audit dag runs these pieces in order: pick the window, read the
bound from the run manifest, prove the bound selects what production read,
rebuild into a throwaway schema, compare.

No pandas here on purpose. The answers are a handful of counts.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from gridlens import timeaxis

PARIS = ZoneInfo("Europe/Paris")

# Has to match the prefix scripts/dbt_teardown.py will agree to drop.
AUDIT_PREFIX = "gridlens_audit_"

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# What stg_generation reads from bronze, and so everything production could
# have seen. The clock check compares exactly these.
BRONZE_COLUMNS = (
    "source",
    "source_document_id",
    "zone",
    "production_type",
    "direction",
    "unit",
    "resolution_minutes",
    "valid_time",
    "known_at",
    "source_updated_at",
    "quantity_mw",
)

# Every table the restated build materialises, where production keeps it, and
# what confines production to the same window. The two views are not compared
# because they are not data. run_manifest is not built at all: it records a
# run, and two runs are never the same run.
COMPARED = (
    ("generation_versions", "gridlens_silver", "valid_time"),
    ("period_generation_net", "gridlens_gold", "valid_time"),
    ("daily_generation_mix", "gridlens_gold", "settlement_day"),
)

LATEST_RUN = """
SELECT
    git_sha,
    CAST(bronze_snapshot_id AS varchar),
    CAST(bronze_committed_at AS varchar)
FROM gridlens_gold.run_manifest
ORDER BY run_started_at DESC
LIMIT 1
"""

# Both directions of EXCEPT, because one direction only proves a subset. The
# counts sit beside them because EXCEPT is a set operation and would not notice
# a row that appears twice on one side.
CLOCKS = """
WITH at_snapshot AS (
    SELECT {columns}
    FROM gridlens_bronze.generation FOR VERSION AS OF {snapshot_id}
    WHERE valid_time >= TIMESTAMP '{start}' AND valid_time < TIMESTAMP '{end}'
),
by_known_at AS (
    SELECT {columns}
    FROM gridlens_bronze.generation
    WHERE known_at <= TIMESTAMP '{as_of}'
      AND valid_time >= TIMESTAMP '{start}' AND valid_time < TIMESTAMP '{end}'
)
SELECT
    (SELECT count(*) FROM at_snapshot),
    (SELECT count(*) FROM by_known_at),
    (SELECT count(*) FROM (SELECT * FROM at_snapshot EXCEPT SELECT * FROM by_known_at)),
    (SELECT count(*) FROM (SELECT * FROM by_known_at EXCEPT SELECT * FROM at_snapshot))
"""

DIFF = """
WITH restated AS (
    SELECT * FROM {audit_schema}.{table} WHERE {predicate}
),
production AS (
    SELECT * FROM {production_schema}.{table} WHERE {predicate}
)
SELECT
    (SELECT count(*) FROM restated),
    (SELECT count(*) FROM production),
    (SELECT count(*) FROM (SELECT * FROM restated EXCEPT SELECT * FROM production)),
    (SELECT count(*) FROM (SELECT * FROM production EXCEPT SELECT * FROM restated))
"""

COUNTS = ("left", "right", "only_left", "only_right")


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_results(self, **kwargs: Any) -> Any: ...


class AuditFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    first_day: date
    last_day: date
    start: datetime
    end: datetime

    def valid_time_predicate(self) -> str:
        return (
            f"valid_time >= TIMESTAMP '{sql_timestamp(self.start)}' "
            f"AND valid_time < TIMESTAMP '{sql_timestamp(self.end)}'"
        )

    def settlement_day_predicate(self) -> str:
        return (
            f"settlement_day BETWEEN DATE '{self.first_day.isoformat()}' "
            f"AND DATE '{self.last_day.isoformat()}'"
        )


@dataclass(frozen=True)
class ProductionRun:
    git_sha: str
    snapshot_id: int
    learned_up_to: datetime


def sql_timestamp(moment: datetime) -> str:
    """The utc wall clock, which is what every timestamp in the lake holds.

    Athena has no zone aware timestamps, so utc is a convention the contract
    layer holds up, and the literal has to follow it.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def last_month(today: date) -> Window:
    """Every Paris settlement day of the calendar month before today's."""
    last_day = today.replace(day=1) - timedelta(days=1)
    first_day = last_day.replace(day=1)
    # Paris boundaries, not utc midnight. A month cut at 00:00Z starts and
    # ends with a partial settlement day, and both would differ from
    # production for reasons that have nothing to do with reproducibility.
    start, _ = timeaxis.settlement_day(first_day, PARIS)
    _, end = timeaxis.settlement_day(last_day, PARIS)
    return Window(first_day, last_day, start, end)


def schema_for(run_day: date) -> str:
    return f"{AUDIT_PREFIX}{run_day:%Y%m%d}"


def _rows(
    client: QueryEngine, sql: str, *, workgroup: str, sleep: Any
) -> list[list[str]]:
    query_id = client.start_query_execution(QueryString=sql, WorkGroup=workgroup)[
        "QueryExecutionId"
    ]
    while True:
        execution = client.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        state = execution["Status"]["State"]
        if state in TERMINAL:
            break
        sleep(1)

    if state != "SUCCEEDED":
        raise AuditFailed(execution["Status"].get("StateChangeReason", "no reason"))

    result = client.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    return [
        [cell.get("VarCharValue", "") for cell in row["Data"]] for row in result[1:]
    ]


def _counts(row: list[str]) -> dict[str, int]:
    return dict(zip(COUNTS, (int(value) for value in row), strict=True))


def _identical(counts: dict[str, int]) -> bool:
    return (
        counts["left"] == counts["right"]
        and counts["only_left"] == 0
        and counts["only_right"] == 0
    )


def production_run(
    client: QueryEngine, *, workgroup: str = "gridlens", sleep: Any = time.sleep
) -> ProductionRun:
    """What the newest production build read, from its manifest row."""
    rows = _rows(client, LATEST_RUN, workgroup=workgroup, sleep=sleep)
    if not rows:
        raise AuditFailed("run_manifest is empty, so there is no production to audit")
    git_sha, snapshot_id, committed_at = rows[0]

    # both go into sql further on. parsing them is the check that they are a
    # number and a timestamp and nothing else.
    if not snapshot_id.isdigit():
        raise AuditFailed(f"snapshot id {snapshot_id!r} is not a number")
    return ProductionRun(
        git_sha=git_sha,
        snapshot_id=int(snapshot_id),
        learned_up_to=datetime.fromisoformat(committed_at).replace(tzinfo=UTC),
    )


def check_clocks(
    client: QueryEngine,
    window: Window,
    run: ProductionRun,
    *,
    workgroup: str = "gridlens",
    sleep: Any = time.sleep,
) -> dict[str, int]:
    """Prove that known_at <= the bound selects exactly what production read.

    The restated build filters on known_at, the clock this project promises.
    Production read an iceberg snapshot. The two only agree while every row's
    known_at precedes its commit, and known_at is an input, so a backfill
    committed after production ran but stamped earlier would be in the
    restatement and not in production. This is where that shows up, rather
    than as a mysterious difference in gold.
    """
    sql = CLOCKS.format(
        columns=", ".join(BRONZE_COLUMNS),
        snapshot_id=run.snapshot_id,
        start=sql_timestamp(window.start),
        end=sql_timestamp(window.end),
        as_of=sql_timestamp(run.learned_up_to),
    )
    try:
        rows = _rows(client, sql, workgroup=workgroup, sleep=sleep)
    except AuditFailed as exc:
        raise AuditFailed(
            f"clock check failed: {exc}. If snapshot {run.snapshot_id} has been "
            "expired, rebuild production first so the manifest names a live one."
        ) from exc

    counts = _counts(rows[0])
    if not _identical(counts):
        raise AuditFailed(
            f"known_at and snapshot {run.snapshot_id} disagree about what "
            f"production read: {counts}"
        )
    return counts


def restate(
    dbt: str,
    project_dir: str,
    schema: str,
    window: Window,
    run: ProductionRun,
    *,
    artifacts_dir: str | None = None,
    runner: Any = subprocess.run,
) -> str:
    """Build silver and gold into the audit schema, bounded at the run's bound.

    artifacts_dir moves dbt's target/ and logs/ out of the project. The
    container needs it: the project is the host's, and a parse cache written by
    dbt on windows keys files by backslash paths, which crashed dbt on linux
    with a KeyError on the first run. Writing its own cache there would break
    the host's next build the same way.
    """
    variables = {
        "restate_as_of": sql_timestamp(run.learned_up_to),
        "restate_from": sql_timestamp(window.start),
        "restate_to": sql_timestamp(window.end),
    }
    command = [
        dbt,
        "run",
        "--project-dir",
        project_dir,
        "--profiles-dir",
        project_dir,
        "--target",
        "audit",
        "--exclude",
        "run_manifest",
        "--vars",
        json.dumps(variables),
    ]
    if artifacts_dir:
        command += [
            "--target-path",
            f"{artifacts_dir}/target",
            "--log-path",
            f"{artifacts_dir}/logs",
        ]
    result = runner(
        command,
        env={**os.environ, "DBT_AUDIT_SCHEMA": schema},
        capture_output=True,
        text=True,
    )
    output: str = result.stdout + result.stderr
    if result.returncode != 0:
        raise AuditFailed(f"dbt exited {result.returncode}\n{output}")
    return output


def compare(
    client: QueryEngine,
    schema: str,
    window: Window,
    *,
    workgroup: str = "gridlens",
    sleep: Any = time.sleep,
) -> dict[str, dict[str, int]]:
    """Row for row, every column, restated against production, per table."""
    results: dict[str, dict[str, int]] = {}
    for table, production_schema, column in COMPARED:
        predicate = (
            window.valid_time_predicate()
            if column == "valid_time"
            else window.settlement_day_predicate()
        )
        sql = DIFF.format(
            audit_schema=schema,
            production_schema=production_schema,
            table=table,
            predicate=predicate,
        )
        rows = _rows(client, sql, workgroup=workgroup, sleep=sleep)
        counts = _counts(rows[0])
        results[table] = {
            "restated": counts["left"],
            "production": counts["right"],
            "only_restated": counts["only_left"],
            "only_production": counts["only_right"],
        }
    return results


def differences(results: dict[str, dict[str, int]]) -> list[str]:
    """The tables where the rebuild did not land on production."""
    return [
        table
        for table, counts in results.items()
        if counts["restated"] != counts["production"]
        or counts["only_restated"]
        or counts["only_production"]
    ]
