#!/usr/bin/env python
"""Drop a pull request's dbt schema, every relation in it, and its data."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3

# The only prefix the ci role may create or drop. Checked here as well as in
# iam, because a teardown pointed at gridlens_silver by a typo should fail in
# this script rather than rely on a policy denying it.
PREFIX = "gridlens_ci_"

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class Catalog(Protocol):
    def get_paginator(self, name: str, /) -> Any: ...
    def delete_database(self, **kwargs: Any) -> Any: ...


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...


class ObjectStore(Protocol):
    def get_paginator(self, name: str, /) -> Any: ...
    def delete_objects(self, **kwargs: Any) -> Any: ...


class RefusedSchema(ValueError):
    pass


class DropFailed(RuntimeError):
    pass


def check_schema(name: str) -> None:
    """Refuse anything that is not a ci schema with a plain name."""
    if not name.startswith(PREFIX) or name == PREFIX:
        raise RefusedSchema(f"{name!r} is not a {PREFIX} schema, refusing to drop it")
    # the name goes into sql, so it must be exactly what the workflow generated
    if not all(c.isalnum() or c == "_" for c in name):
        raise RefusedSchema(f"{name!r} contains characters a ci schema never has")


def _missing(exc: Exception) -> bool:
    response: dict[str, Any] = getattr(exc, "response", {}) or {}
    return bool(response.get("Error", {}).get("Code") == "EntityNotFoundException")


def relations(catalog: Catalog, database: str) -> list[tuple[str, bool]] | None:
    """Every relation as (name, is_view), or None when the schema is gone."""
    found: list[tuple[str, bool]] = []
    try:
        for page in catalog.get_paginator("get_tables").paginate(DatabaseName=database):
            for table in page.get("TableList", []):
                found.append((table["Name"], table.get("TableType") == "VIRTUAL_VIEW"))
    except Exception as exc:
        if _missing(exc):
            return None
        raise
    return found


def _run(
    engine: QueryEngine, sql: str, workgroup: str, sleep: Any, poll_seconds: float
) -> None:
    query_id = engine.start_query_execution(QueryString=sql, WorkGroup=workgroup)[
        "QueryExecutionId"
    ]
    while True:
        execution = engine.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        state = execution["Status"]["State"]
        if state in TERMINAL:
            break
        sleep(poll_seconds)
    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "no reason given")
        raise DropFailed(f"{sql}: {reason}")


def sweep(store: ObjectStore, data_prefix: str, database: str) -> int:
    """Delete leftover objects under the schema's own data prefix."""
    parsed = urlparse(data_prefix)
    bucket = parsed.netloc
    # schema_table_unique puts every table under <data dir>/<schema>/, so this
    # prefix cannot reach another schema's data
    prefix = parsed.path.lstrip("/").rstrip("/") + f"/{database}/"
    deleted = 0
    for page in store.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        keys = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
        if keys:
            store.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
    return deleted


def teardown(
    catalog: Catalog,
    engine: QueryEngine,
    database: str,
    *,
    workgroup: str = "gridlens",
    store: ObjectStore | None = None,
    data_prefix: str | None = None,
    sleep: Any = time.sleep,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    check_schema(database)

    found = relations(catalog, database)
    dropped: list[str] = []
    if found is not None:
        for name, is_view in found:
            check_schema(database)
            # through athena rather than glue.delete_table. dropping an iceberg
            # table in athena deletes its data, where deleting the catalog
            # entry alone leaves every file behind with nothing pointing at it.
            kind = "VIEW" if is_view else "TABLE"
            _run(
                engine,
                f"DROP {kind} IF EXISTS `{database}`.`{name}`",
                workgroup,
                sleep,
                poll_seconds,
            )
            dropped.append(name)
        catalog.delete_database(Name=database)

    swept = sweep(store, data_prefix, database) if store and data_prefix else 0
    return {
        "schema": database,
        "existed": found is not None,
        "dropped": dropped,
        "objects_swept": swept,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schema")
    parser.add_argument("--data-prefix", help="the ci target's s3_data_dir")
    parser.add_argument("--workgroup", default="gridlens")
    parser.add_argument("--region", default="eu-west-3")
    args = parser.parse_args()

    try:
        result = teardown(
            boto3.client("glue", region_name=args.region),
            boto3.client("athena", region_name=args.region),
            args.schema,
            workgroup=args.workgroup,
            store=boto3.client("s3", region_name=args.region),
            data_prefix=args.data_prefix,
        )
    except RefusedSchema as exc:
        print(exc, file=sys.stderr)
        return 2

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
