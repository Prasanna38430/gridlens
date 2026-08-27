from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from table_stats import (  # noqa: E402
    QueryFailed,
    classify,
    format_report,
    group_by_class,
    run_query,
)

ROOT = "bronze/generation"


class FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.pages


class FakeAthena:
    def __init__(self, states: list[str], pages: list[dict[str, Any]]) -> None:
        self.states = states
        self.pages = pages
        self.polls = 0

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": "it did not work"}
            }
        }

    def get_paginator(self, name: str) -> FakePaginator:
        return FakePaginator(self.pages)


def result_page(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    return {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": v} for v in values]}
                for values in [header, *rows]
            ]
        }
    }


def test_rows_come_back_keyed_by_column_and_the_header_is_dropped():
    client = FakeAthena(
        ["RUNNING", "SUCCEEDED"],
        [result_page(["files", "rows"], [["51", "33548"]])],
    )
    rows = run_query(
        client, "SELECT 1", database="d", workgroup="w", sleep=lambda _: None
    )
    assert rows == [{"files": "51", "rows": "33548"}]


def test_a_failed_query_raises_with_athenas_reason():
    client = FakeAthena(["FAILED"], [])
    with pytest.raises(QueryFailed, match="it did not work"):
        run_query(client, "SELECT 1", database="d", workgroup="w", sleep=lambda _: None)


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        (f"{ROOT}/metadata/00028-33ef.metadata.json", "metadata json"),
        (f"{ROOT}/metadata/snap-8931128310435805817-1-f151.avro", "manifest lists"),
        (f"{ROOT}/metadata/1b76bfce-622b-478e-97e4-b072bf54fdfc-m0.avro", "manifests"),
        (f"{ROOT}/data/j1Flgg/zone=10YFR/valid_time_day=2026-08-18/x.parquet", "data"),
    ],
)
def test_objects_sort_into_storage_classes(key: str, kind: str):
    assert classify(key) == kind


def test_a_manifest_named_like_a_snapshot_is_still_a_manifest():
    # object storage mode hashes the data prefix, so a data key can contain the
    # word snap. only the file name decides.
    assert classify(f"{ROOT}/data/snap-x/part-m0.parquet") == "data"


def test_bytes_and_counts_add_up_per_class():
    grouped = group_by_class(
        {
            f"{ROOT}/data/a.parquet": 100,
            f"{ROOT}/data/b.parquet": 200,
            f"{ROOT}/metadata/snap-1.avro": 50,
        }
    )
    assert grouped == {"data": (2, 300), "manifest lists": (1, 50)}


def stats() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    return (
        {
            "files": "51",
            "rows": "33548",
            "bytes": "260312",
            "smallest": "1840",
            "largest": "8026",
            "mean": "5104",
        },
        {"partitions": "22", "most_files": "5", "mean_files": "2.32"},
        {"snapshots": "28", "oldest": "2026-08-22", "newest": "2026-08-26"},
    )


def test_the_report_states_the_metadata_to_data_ratio():
    files, partitions, snapshots = stats()
    report = format_report(
        "gridlens_bronze.generation",
        files,
        partitions,
        snapshots,
        {"data": (52, 100), "manifests": (28, 200), "metadata json": (29, 100)},
        [],
    )
    assert "metadata is 3.0x the data it describes" in report
    assert "every data object is in the current snapshot" in report


def test_unreferenced_objects_are_listed_rather_than_counted():
    files, partitions, snapshots = stats()
    stray = "s3://bucket/bronze/generation/data/j1Flgg/zone=10YFR/x.parquet"
    report = format_report(
        "gridlens_bronze.generation",
        files,
        partitions,
        snapshots,
        {"data": (52, 100)},
        [stray],
    )
    assert "1 data object is not in the current snapshot" in report
    assert stray in report


def test_an_empty_table_does_not_divide_by_zero():
    files, partitions, snapshots = stats()
    report = format_report(
        "gridlens_bronze.generation", files, partitions, snapshots, {}, []
    )
    assert "metadata is 0.0x the data it describes" in report
