#!/usr/bin/env python
"""Report what an Iceberg table costs to keep: files, snapshots, metadata."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# Athena exposes a subset of the metadata tables the Iceberg spec describes.
# $all_files, $delete_files and $position_deletes are not among them, and
# $files holds data files only. A delete file is therefore invisible from SQL,
# and the counts for those come out of the snapshot summary instead.
FILE_STATS = """
SELECT
    count(*) AS files,
    sum(record_count) AS rows,
    sum(file_size_in_bytes) AS bytes,
    min(file_size_in_bytes) AS smallest,
    max(file_size_in_bytes) AS largest,
    cast(round(avg(file_size_in_bytes)) AS bigint) AS mean
FROM "{table}$files"
"""

PARTITION_STATS = """
SELECT
    count(*) AS partitions,
    max(file_count) AS most_files,
    cast(round(avg(file_count), 2) AS varchar) AS mean_files
FROM "{table}$partitions"
"""

SNAPSHOT_STATS = """
SELECT
    count(*) AS snapshots,
    cast(min(committed_at) AS varchar) AS oldest,
    cast(max(committed_at) AS varchar) AS newest
FROM "{table}$snapshots"
"""

LIVE_FILES = 'SELECT file_path FROM "{table}$files"'

# Answered from manifests, so it scans nothing. It differs from the sum of
# record_count when delete files are masking rows, and that difference is the
# only signal Athena gives that any exist.
LIVE_ROWS = "SELECT count(*) AS rows FROM {table}"

# How many unaccounted paths to name before the listing stops being readable.
LISTING_LIMIT = 5


class QueryEngine(Protocol):
    def start_query_execution(self, **kwargs: Any) -> Any: ...
    def get_query_execution(self, **kwargs: Any) -> Any: ...
    def get_paginator(self, name: str) -> Any: ...


class ObjectStore(Protocol):
    def get_paginator(self, name: str) -> Any: ...
    def get_object(self, **kwargs: Any) -> Any: ...


class Catalog(Protocol):
    def get_table(self, **kwargs: Any) -> Any: ...


class QueryFailed(RuntimeError):
    pass


def run_query(
    client: QueryEngine,
    sql: str,
    *,
    database: str,
    workgroup: str,
    poll_seconds: float = 1.0,
    sleep: Any = time.sleep,
) -> list[dict[str, str]]:
    """Run one statement and return its rows keyed by column name."""
    query_id = client.start_query_execution(
        QueryString=sql.strip(),
        QueryExecutionContext={"Database": database},
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
        raise QueryFailed(execution["Status"].get("StateChangeReason", "no reason"))

    header: list[str] = []
    rows: list[dict[str, str]] = []
    for page in client.get_paginator("get_query_results").paginate(
        QueryExecutionId=query_id
    ):
        for row in page["ResultSet"]["Rows"]:
            values = [cell.get("VarCharValue", "") for cell in row["Data"]]
            if not header:
                header = values
                continue
            rows.append(dict(zip(header, values, strict=False)))
    return rows


def classify(key: str) -> str:
    """Sort one object under the table root into a storage class."""
    name = key.rsplit("/", 1)[-1]
    if name.endswith(".metadata.json"):
        return "metadata json"
    if name.startswith("snap-") and name.endswith(".avro"):
        return "manifest lists"
    if name.endswith(".avro"):
        return "manifests"
    return "data"


def group_by_class(objects: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Object count and total bytes per storage class."""
    grouped: dict[str, tuple[int, int]] = {}
    for key, size in objects.items():
        kind = classify(key)
        count, total = grouped.get(kind, (0, 0))
        grouped[kind] = (count + 1, total + size)
    return grouped


def list_objects(store: ObjectStore, bucket: str, prefix: str) -> dict[str, int]:
    objects: dict[str, int] = {}
    for page in store.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for entry in page.get("Contents", []):
            objects[entry["Key"]] = entry["Size"]
    return objects


def metadata_location(catalog: Catalog, database: str, table: str) -> str:
    """The current metadata json, read from the catalog rather than guessed."""
    parameters = catalog.get_table(DatabaseName=database, Name=table)["Table"].get(
        "Parameters", {}
    )
    location: str = parameters.get("metadata_location", "")
    if not location:
        raise QueryFailed(f"{database}.{table} has no metadata_location")
    return location


def table_root(location: str) -> str:
    return location.rsplit("/metadata/", 1)[0] + "/"


def current_summary(store: ObjectStore, bucket: str, key: str) -> dict[str, str]:
    """The newest snapshot's summary, which is where delete file counts live."""
    document = json.loads(store.get_object(Bucket=bucket, Key=key)["Body"].read())
    snapshots = document.get("snapshots", [])
    if not snapshots:
        return {}
    summary: dict[str, str] = snapshots[-1].get("summary", {})
    return summary


def format_report(
    table: str,
    files: dict[str, str],
    partitions: dict[str, str],
    snapshots: dict[str, str],
    summary: dict[str, str],
    live_rows: str,
    storage: dict[str, tuple[int, int]],
    unmatched: list[str],
) -> str:
    parquet_bytes = storage.get("data", (0, 0))[1]
    metadata_bytes = sum(
        total for kind, (_, total) in storage.items() if kind != "data"
    )
    ratio = metadata_bytes / parquet_bytes if parquet_bytes else 0.0

    deletes = int(summary.get("total-delete-files", 0))
    positions = int(summary.get("total-position-deletes", 0))
    masked = int(files["rows"]) - int(live_rows)

    rows = f"  rows          {live_rows} live"
    if masked:
        rows += f", {files['rows']} in data files, {masked} masked by deletes"

    lines = [
        table,
        "",
        rows,
        f"  data files    {files['files']}",
        f"  delete files  {deletes}, holding {positions} position"
        f" {'delete' if positions == 1 else 'deletes'}",
        f"  file size     {files['smallest']} min, {files['mean']} mean,"
        f" {files['largest']} max",
        f"  partitions    {partitions['partitions']},"
        f" {partitions['mean_files']} files each on average,"
        f" {partitions['most_files']} at most",
        f"  snapshots     {snapshots['snapshots']},"
        f" {snapshots['oldest']} to {snapshots['newest']}",
        "",
        "  storage",
    ]
    for kind, label in (
        ("data", "parquet"),
        ("manifests", "manifests"),
        ("manifest lists", "manifest lists"),
        ("metadata json", "metadata json"),
    ):
        count, total = storage.get(kind, (0, 0))
        lines.append(f"    {label:<15}{count:>5} objects{total:>10} bytes")
    lines.append(f"    metadata is {ratio:.1f}x the parquet it describes")
    lines.append("")

    # Delete files are parquet under the same data prefix and $files does not
    # list them, so subtracting the summary's count is the only way to keep
    # them out of the unaccounted pile. What is left over is either held by an
    # older snapshot, which expiry releases, or orphaned by a commit that never
    # landed, which expiry will never touch.
    leftover = len(unmatched) - deletes
    on_disk = storage.get("data", (0, 0))[0]
    lines.append(
        f"  {on_disk} parquet objects under data/: {files['files']} in the current"
        f" snapshot, {deletes} delete, {leftover} unaccounted"
    )
    if leftover > 0:
        lines.append("  unaccounted objects are held by an older snapshot or orphaned")
        lines.extend(f"    {key}" for key in unmatched[:LISTING_LIMIT])
        if len(unmatched) > LISTING_LIMIT:
            lines.append(f"    and {len(unmatched) - LISTING_LIMIT} more")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", nargs="?", default="generation")
    parser.add_argument("--database", default="gridlens_bronze")
    parser.add_argument("--workgroup", default="gridlens")
    parser.add_argument("--region", default="eu-west-3")
    args = parser.parse_args()

    athena = boto3.client("athena", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    glue = boto3.client("glue", region_name=args.region)

    def query(sql: str) -> list[dict[str, str]]:
        return run_query(
            athena,
            sql.format(table=args.table),
            database=args.database,
            workgroup=args.workgroup,
        )

    files = query(FILE_STATS)[0]
    partitions = query(PARTITION_STATS)[0]
    snapshots = query(SNAPSHOT_STATS)[0]
    live_rows = query(LIVE_ROWS)[0]["rows"]
    live = {row["file_path"] for row in query(LIVE_FILES)}

    location = metadata_location(glue, args.database, args.table)
    parsed = urlparse(table_root(location))
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    objects = list_objects(s3, bucket, prefix)
    summary = current_summary(s3, bucket, urlparse(location).path.lstrip("/"))

    unmatched = sorted(
        f"s3://{bucket}/{key}"
        for key in objects
        if classify(key) == "data" and f"s3://{bucket}/{key}" not in live
    )

    print(
        format_report(
            f"{args.database}.{args.table}",
            files,
            partitions,
            snapshots,
            summary,
            live_rows,
            group_by_class(objects),
            unmatched,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
