from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Protocol

SQL_TIMESTAMP = "%Y-%m-%d %H:%M:%S.%f"

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

COLUMNS = (
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

# Every column is cast explicitly. The staging table is all strings on purpose:
# a json serde guessing at types is a source of silent nulls, and an explicit
# cast fails loudly on the row that is wrong.
_STAGED = """
    SELECT
        source,
        source_document_id,
        zone,
        production_type,
        direction,
        unit,
        CAST(resolution_minutes AS integer) AS resolution_minutes,
        CAST(valid_time AS timestamp) AS valid_time,
        CAST(known_at AS timestamp) AS known_at,
        CAST(source_updated_at AS timestamp) AS source_updated_at,
        CAST(quantity_mw AS decimal(12, 3)) AS quantity_mw
    FROM {database}.staging_generation
    WHERE batch_id = '{batch_id}'
"""

# WHEN NOT MATCHED THEN INSERT, and deliberately no WHEN MATCHED clause.
# Bronze never updates. If a row with this identity is already here then the
# correct action is nothing, which is exactly what makes a rerun a no-op:
# athena does not even cut a snapshot when a merge inserts zero rows.
#
# The bound on t.valid_time is a partition pruning hint. The join predicate
# alone gives athena no constant to prune on, so without it the merge reads
# every day in the table to find matches for one.
MERGE = """
MERGE INTO {database}.generation t
USING (
{staged}
) s
ON  t.zone = s.zone
AND t.valid_time = s.valid_time
AND t.known_at = s.known_at
AND t.source = s.source
AND t.production_type = s.production_type
AND t.direction = s.direction
AND t.valid_time >= TIMESTAMP '{window_start}'
AND t.valid_time <  TIMESTAMP '{window_end}'
WHEN NOT MATCHED THEN INSERT ({columns})
VALUES ({values})
"""


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...


class BronzeLoadFailed(RuntimeError):
    pass


def merge_batch(
    client: QueryEngine,
    batch_id: str,
    window_start: datetime,
    window_end: datetime,
    *,
    database: str = "gridlens_bronze",
    workgroup: str = "gridlens",
    poll_seconds: float = 1.0,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Merge one staged batch into bronze. Running it again inserts nothing."""
    if not batch_id:
        raise ValueError("batch_id is required")
    # the batch id goes into sql, so it must be exactly what we generated
    if not batch_id.isalnum():
        raise ValueError(f"batch_id is not a plain hex string: {batch_id!r}")
    for label, moment in (("window_start", window_start), ("window_end", window_end)):
        if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError(f"{label} must be utc aware")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    sql = MERGE.format(
        database=database,
        staged=_STAGED.format(database=database, batch_id=batch_id),
        columns=", ".join(COLUMNS),
        values=", ".join(f"s.{c}" for c in COLUMNS),
        window_start=window_start.strftime(SQL_TIMESTAMP),
        window_end=window_end.strftime(SQL_TIMESTAMP),
    )

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
        raise BronzeLoadFailed(
            execution["Status"].get("StateChangeReason", "no reason given")
        )

    stats = execution.get("Statistics", {})
    return {
        "query_id": query_id,
        "scanned_bytes": stats.get("DataScannedInBytes", 0),
        "millis": stats.get("TotalExecutionTimeInMillis", 0),
    }
