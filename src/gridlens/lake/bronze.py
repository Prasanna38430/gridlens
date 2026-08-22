from __future__ import annotations

import time
from typing import Any, Protocol

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# Every column is cast explicitly. The staging table is all strings on purpose:
# a json serde guessing at types is a source of silent nulls, and an explicit
# cast fails loudly on the row that is wrong.
INSERT = """
INSERT INTO {database}.generation
SELECT
    source,
    source_document_id,
    zone,
    production_type,
    direction,
    unit,
    CAST(resolution_minutes AS integer),
    CAST(valid_time AS timestamp),
    CAST(known_at AS timestamp),
    CAST(source_updated_at AS timestamp),
    CAST(quantity_mw AS decimal(12, 3))
FROM {database}.staging_generation
WHERE batch_id = '{batch_id}'
"""


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...


class BronzeLoadFailed(RuntimeError):
    pass


def load_batch(
    client: QueryEngine,
    batch_id: str,
    *,
    database: str = "gridlens_bronze",
    workgroup: str = "gridlens",
    poll_seconds: float = 1.0,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Append one staged batch to bronze and report what it cost."""
    if not batch_id:
        raise ValueError("batch_id is required")
    # the batch id goes into sql, so it must be exactly what we generated
    if not batch_id.isalnum():
        raise ValueError(f"batch_id is not a plain hex string: {batch_id!r}")

    query_id = client.start_query_execution(
        QueryString=INSERT.format(database=database, batch_id=batch_id),
        WorkGroup=workgroup,
    )["QueryExecutionId"]

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
