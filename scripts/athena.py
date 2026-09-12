#!/usr/bin/env python
"""Run SQL files through Athena and report what each one cost."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import boto3

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def strip_leading_comments(sql: str) -> str:
    """Drop comment and blank lines before the statement.

    Athena routes its Iceberg extensions on the first keyword it sees, so a
    header comment in front of OPTIMIZE or VACUUM gets the statement parsed as
    ordinary SQL and rejected with "mismatched input". Every other statement
    tolerates the comment, which is why this went unnoticed: the maintenance
    files were verified by running the statements, not the files.
    """
    lines = sql.splitlines()
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            start = index
            break
    else:
        start = len(lines)
    return "\n".join(lines[start:]).strip()


def run_statement(
    client: Any, sql: str, workgroup: str, database: str
) -> dict[str, Any]:
    query_id = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )["QueryExecutionId"]

    while True:
        execution = client.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        if execution["Status"]["State"] in TERMINAL:
            return dict(execution)
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--workgroup", default="gridlens")
    parser.add_argument("--database", default="gridlens_bronze")
    parser.add_argument("--region", default="eu-west-3")
    args = parser.parse_args()

    client = boto3.client("athena", region_name=args.region)
    failed = False

    for path in args.files:
        # one statement per file. athena takes one per call and splitting on
        # semicolons breaks the moment a string literal contains one.
        sql = strip_leading_comments(path.read_text(encoding="utf-8")).rstrip(";")
        execution = run_statement(client, sql, args.workgroup, args.database)
        status = execution["Status"]
        stats = execution.get("Statistics", {})

        scanned = stats.get("DataScannedInBytes", 0)
        millis = stats.get("TotalExecutionTimeInMillis", 0)
        print(f"{path.name}: {status['State']}  {millis} ms  {scanned} bytes scanned")

        if status["State"] != "SUCCEEDED":
            print(
                f"  {status.get('StateChangeReason', 'no reason given')}",
                file=sys.stderr,
            )
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
