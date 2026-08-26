#!/usr/bin/env python
"""Run the bronze expectation suite and exit non-zero on any failure."""

from __future__ import annotations

import argparse
import sys

import boto3

from gridlens.quality.bronze import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="gridlens_bronze")
    parser.add_argument("--workgroup", default="gridlens")
    parser.add_argument("--region", default="eu-west-3")
    args = parser.parse_args()

    result = run(
        boto3.client("athena", region_name=args.region),
        database=args.database,
        workgroup=args.workgroup,
    )
    print(
        f"{result['expectations']} expectations, "
        f"{len(result['failed'])} failed, "
        f"{result['scanned_bytes']} bytes scanned"
    )
    for failure in result["failed"]:
        print(f"  {failure}", file=sys.stderr)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
