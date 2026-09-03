"""Which settlement days bronze is missing over a window.

Deliberately free of pandas and great expectations so this can run anywhere
the ingest can, including a lambda. The quality suite answers a different
question: it says the newest day is late, not which older days never arrived.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any, Protocol

SQL_DATE = "%Y-%m-%d"

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# The left join is against a generated day series rather than against min and
# max of what is present, because a hole in the middle and a short tail are the
# same bug and only one of them shows up in a range.
#
# The literal bounds on valid_time are not decoration. A predicate only on the
# derived Paris date gives Athena no constant to prune on and it reads the
# whole table.
MISSING_DAYS = """
WITH wanted AS (
    -- sequence over a day interval yields timestamps, not dates, so this cast
    -- is what stops the result reading back as 2026-07-28 00:00:00
    SELECT CAST(d AS date) AS d
    FROM UNNEST(sequence(DATE '{start}', DATE '{end}', INTERVAL '1' DAY)) AS t (d)
),
present AS (
    SELECT DISTINCT CAST(valid_time AT TIME ZONE 'Europe/Paris' AS date) AS d
    FROM {database}.generation
    WHERE valid_time >= TIMESTAMP '{lower} 00:00:00'
      AND valid_time <  TIMESTAMP '{upper} 00:00:00'
)
SELECT CAST(wanted.d AS varchar) AS missing_day
FROM wanted
LEFT JOIN present ON present.d = wanted.d
WHERE present.d IS NULL
ORDER BY 1
"""


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_results(self, **kwargs: Any) -> Any: ...


class GapQueryFailed(RuntimeError):
    pass


def _rows(
    client: QueryEngine,
    sql: str,
    *,
    workgroup: str,
    sleep: Any,
    poll_seconds: float,
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
        sleep(poll_seconds)

    if state != "SUCCEEDED":
        raise GapQueryFailed(execution["Status"].get("StateChangeReason", "no reason"))

    result = client.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    # the first row is the header, always, even when nothing matched
    return [
        [cell.get("VarCharValue", "") for cell in row["Data"]] for row in result[1:]
    ]


def missing_settlement_days(
    client: QueryEngine,
    *,
    start: date,
    end: date,
    database: str = "gridlens_bronze",
    workgroup: str = "gridlens",
    poll_seconds: float = 1.0,
    sleep: Any = time.sleep,
) -> list[date]:
    """Settlement days in [start, end] with no rows at all in bronze."""
    if end < start:
        raise ValueError("end is before start")

    sql = MISSING_DAYS.format(
        start=start.strftime(SQL_DATE),
        end=end.strftime(SQL_DATE),
        database=database,
        # a paris day runs 22:00Z to 22:00Z, so widen a day either side rather
        # than trying to be clever about the offset on any given date
        lower=(start - timedelta(days=1)).strftime(SQL_DATE),
        upper=(end + timedelta(days=2)).strftime(SQL_DATE),
    )
    rows = _rows(
        client, sql, workgroup=workgroup, sleep=sleep, poll_seconds=poll_seconds
    )
    return [date.fromisoformat(row[0]) for row in rows]
