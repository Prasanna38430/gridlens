from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from table_stats import (  # noqa: E402
    LISTING_LIMIT,
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


def report(
    summary: dict[str, str] | None = None,
    live_rows: str = "33548",
    storage: dict[str, tuple[int, int]] | None = None,
    unmatched: list[str] | None = None,
) -> str:
    files, partitions, snapshots = stats()
    return format_report(
        "gridlens_bronze.generation",
        files,
        partitions,
        snapshots,
        summary if summary is not None else {},
        live_rows,
        storage if storage is not None else {"data": (51, 100)},
        unmatched if unmatched is not None else [],
    )


def test_the_report_states_the_metadata_to_parquet_ratio():
    text = report(
        storage={"data": (51, 100), "manifests": (28, 200), "metadata json": (29, 100)}
    )
    assert "metadata is 3.0x the parquet it describes" in text


def test_a_delete_file_is_counted_as_a_delete_not_an_orphan():
    # the bug this replaces: $files hides delete files, so the one masking the
    # day 11 test row was reported as an unreferenced data object.
    stray = "s3://bucket/bronze/generation/data/j1Flgg/zone=10YFR/x.parquet"
    text = report(
        summary={"total-delete-files": "1", "total-position-deletes": "1"},
        live_rows="33547",
        storage={"data": (52, 100)},
        unmatched=[stray],
    )
    assert "52 parquet objects under data/: 51 in the current snapshot" in text
    assert "1 delete, 0 unaccounted" in text
    assert "orphaned" not in text
    assert stray not in text


def test_masked_rows_are_reported_when_deletes_hide_them():
    text = report(summary={"total-delete-files": "1"}, live_rows="33547")
    assert "33547 live, 33548 in data files, 1 masked by deletes" in text


def test_a_table_with_no_deletes_says_so_plainly():
    text = report(live_rows="33548")
    assert "  rows          33548 live\n" in text
    assert "delete files  0, holding 0 position deletes" in text


def test_a_single_position_delete_is_not_pluralised():
    text = report(summary={"total-delete-files": "1", "total-position-deletes": "1"})
    assert "holding 1 position delete\n" in text


def test_a_long_unaccounted_listing_is_capped():
    strays = [f"s3://bucket/bronze/generation/data/x{n}.parquet" for n in range(67)]
    text = report(storage={"data": (89, 100)}, unmatched=strays)
    assert "67 unaccounted" in text
    assert strays[0] in text
    assert strays[LISTING_LIMIT] not in text
    assert f"and {67 - LISTING_LIMIT} more" in text


def test_an_empty_table_does_not_divide_by_zero():
    assert "metadata is 0.0x the parquet it describes" in report(storage={})
