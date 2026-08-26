from __future__ import annotations

import time
from typing import Any, Protocol

import great_expectations as gx
import pandas as pd

from gridlens.quality import queries

# A settlement day is 96 quarter hours, 100 in october and 92 in march. A
# series is allowed to fall short, because entsoe omits positions it has no
# value for and solar does that every night: 70 is the lowest a complete day
# has produced. Below that the fetch itself was short.
MIN_PERIODS_PER_DAY = 70
MAX_PERIODS_PER_DAY = 100

# France peaks near 40 GW on a single production type. 150 GW is the same unit
# error detector the contract applies per row, restated at table level.
MAX_QUANTITY_MW = 150_000.0

# The daily run fires at 06:30 Paris. Two days of silence is a broken schedule
# rather than a slow morning.
MAX_HOURS_SINCE_LAST_LEARNED = 48


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_results(self, **kwargs: Any) -> Any: ...


def fetch(
    client: QueryEngine,
    sql: str,
    *,
    workgroup: str = "gridlens",
    sleep: Any = time.sleep,
) -> tuple[pd.DataFrame, int]:
    """Run a query and return its rows as a frame plus the bytes it scanned."""
    query_id = client.start_query_execution(QueryString=sql, WorkGroup=workgroup)[
        "QueryExecutionId"
    ]
    while True:
        execution = client.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        state = execution["Status"]["State"]
        if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        sleep(1)

    if state != "SUCCEEDED":
        raise RuntimeError(execution["Status"].get("StateChangeReason", "query failed"))

    rows = client.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    header = [c.get("VarCharValue") for c in rows[0]["Data"]]
    body = [[c.get("VarCharValue") for c in r["Data"]] for r in rows[1:]]
    scanned = int(execution.get("Statistics", {}).get("DataScannedInBytes", 0))
    return pd.DataFrame(body, columns=header), scanned


def _numeric(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        out[column] = pd.to_numeric(out[column])
    return out


def _validate(frame: pd.DataFrame, name: str, expectations: list[Any]) -> Any:
    context = gx.get_context(mode="ephemeral")
    batch = (
        context.data_sources.add_pandas(f"{name}-source")
        .add_dataframe_asset(name)
        .add_batch_definition_whole_dataframe("all")
    )
    suite = context.suites.add(gx.ExpectationSuite(name=name))
    for expectation in expectations:
        suite.add_expectation(expectation)

    definition = context.validation_definitions.add(
        gx.ValidationDefinition(name=f"{name}-run", data=batch, suite=suite)
    )
    return definition.run(batch_parameters={"dataframe": frame})


def invariants(frame: pd.DataFrame) -> Any:
    counts = [
        "duplicate_keys",
        "future_periods",
        "known_before_it_happened",
        "negative_quantities",
        "hours_since_last_learned",
    ]
    return _validate(
        _numeric(frame, counts),
        "bronze-invariants",
        [
            # the merge is meant to make this impossible. if it ever fires the
            # writer is broken, not the data.
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="duplicate_keys", min_value=0, max_value=0
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="future_periods", min_value=0, max_value=0
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="known_before_it_happened", min_value=0, max_value=0
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="negative_quantities", min_value=0, max_value=0
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="hours_since_last_learned",
                min_value=0,
                max_value=MAX_HOURS_SINCE_LAST_LEARNED,
            ),
        ],
    )


def completeness(frame: pd.DataFrame) -> Any:
    return _validate(
        _numeric(frame, ["periods", "versions", "max_mw"]),
        "bronze-completeness",
        [
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="periods",
                min_value=MIN_PERIODS_PER_DAY,
                max_value=MAX_PERIODS_PER_DAY,
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="max_mw", min_value=0, max_value=MAX_QUANTITY_MW
            ),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="zone"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="production_type"),
            gx.expectations.ExpectColumnDistinctValuesToBeInSet(
                column="direction", value_set=["generation", "consumption"]
            ),
        ],
    )


def run(
    client: QueryEngine,
    *,
    database: str = "gridlens_bronze",
    workgroup: str = "gridlens",
) -> dict[str, Any]:
    """Validate bronze and report every failed expectation."""
    scanned = 0
    checked = 0
    failures: list[str] = []

    for name, sql, validator in (
        ("invariants", queries.INVARIANTS, invariants),
        ("completeness", queries.COMPLETENESS, completeness),
    ):
        frame, bytes_read = fetch(
            client, sql.format(database=database), workgroup=workgroup
        )
        scanned += bytes_read
        result = validator(frame)
        checked += len(result.results)
        for item in result.results:
            if not item.success:
                column = item.expectation_config.kwargs.get("column")
                observed = item.result.get("partial_unexpected_list")
                failures.append(f"{name}.{column}: {observed}")

    return {
        "expectations": checked,
        "failed": failures,
        "passed": not failures,
        "scanned_bytes": scanned,
    }
