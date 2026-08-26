from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gridlens.contracts.gate import apply_gate
from gridlens.ingest.entsoe_parse import parse_generation
from gridlens.lake.bronze import BronzeLoadFailed, merge_batch
from gridlens.lake.staging import StagingArea, serialise

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
KNOWN_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def records():
    doc = parse_generation((FIXTURES / "a75_fr_20260804.xml").read_bytes())
    return apply_gate(doc, KNOWN_AT).records


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.puts.append(kwargs)
        return {}


class FakeAthena:
    def __init__(self, states: list[str], stats: dict[str, int] | None = None) -> None:
        self.states = states
        self.stats = stats or {
            "DataScannedInBytes": 472090,
            "TotalExecutionTimeInMillis": 1706,
        }
        self.queries: list[str] = []
        self.polls = 0

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.queries.append(kwargs["QueryString"])
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": "it did not work"},
                "Statistics": self.stats,
            }
        }


def test_timestamps_are_written_without_an_offset():
    # athena casts these with a plain CAST, which has no opinion about zones.
    # an offset here would make the cast depend on the session timezone.
    line = json.loads(serialise(records()[:1]).splitlines()[0])
    assert line["valid_time"] == "2026-08-04 00:00:00.000000"
    assert "+" not in line["valid_time"]
    assert line["known_at"] == "2026-08-11 12:00:00.000000"


def test_decimals_keep_their_digits():
    # str() on a Decimal preserves the scale it was parsed with, so this is
    # "288.18" and not "288.180". what matters is that it round trips exactly.
    # athena's CAST to decimal(12,3) does the padding.
    line = json.loads(serialise(records()[:1]).splitlines()[0])
    assert line["quantity_mw"] == "288.18"
    assert Decimal(line["quantity_mw"]) == Decimal("288.180")


def test_a_missing_source_updated_at_stays_null():
    line = json.loads(serialise(records()[:1]).splitlines()[0])
    assert line["source_updated_at"] is None


def test_one_object_per_batch_under_its_own_partition():
    fake = FakeS3()
    batch_id, rows = StagingArea("raw-bucket", fake).write(records())

    assert rows == 1388
    assert len(fake.puts) == 1
    key = fake.puts[0]["Key"]
    assert key == f"staging/generation/batch_id={batch_id}/part.jsonl"
    assert fake.puts[0]["Body"].count(b"\n") == 1388


def test_an_empty_batch_writes_nothing():
    fake = FakeS3()
    assert StagingArea("raw-bucket", fake).write([]) == ("", 0)
    assert fake.puts == []


WINDOW = (
    datetime(2026, 8, 3, 22, tzinfo=UTC),
    datetime(2026, 8, 4, 22, tzinfo=UTC),
)


def test_the_merge_targets_only_the_named_batch():
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    assert "WHERE batch_id = 'abc123'" in sql
    assert "MERGE INTO gridlens_bronze.generation" in sql
    # every column cast explicitly, so a bad value fails rather than nulls out
    assert sql.count("CAST(") == 5


def test_the_merge_never_updates():
    # bronze appends. a WHEN MATCHED clause would let a rerun rewrite history,
    # which is the one thing this table exists not to do.
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "WHEN MATCHED" not in sql.replace("WHEN NOT MATCHED", "")
    assert "UPDATE" not in sql
    assert "DELETE" not in sql


def test_the_merge_keys_on_the_full_bitemporal_identity():
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    for column in (
        "zone",
        "valid_time",
        "known_at",
        "source",
        "production_type",
        "direction",
    ):
        assert f"t.{column} = s.{column}" in sql, column


def test_the_merge_carries_a_constant_bound_for_partition_pruning():
    # the join predicate alone gives athena no constant to prune on, so without
    # this the merge reads every day in the table to match one.
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    assert "t.valid_time >= TIMESTAMP '2026-08-03 22:00:00.000000'" in sql
    assert "t.valid_time <  TIMESTAMP '2026-08-04 22:00:00.000000'" in sql


def test_a_local_window_is_refused():
    athena = FakeAthena(["SUCCEEDED"])
    paris = datetime(2026, 8, 4, tzinfo=ZoneInfo("Europe/Paris"))
    with pytest.raises(ValueError, match="utc aware"):
        merge_batch(
            athena, "abc123", paris, paris + timedelta(days=1), sleep=lambda _: None
        )
    assert athena.queries == []


def test_a_batch_id_that_is_not_plain_hex_is_refused():
    # it goes into a sql string, so anything else is a bug or an injection
    athena = FakeAthena(["SUCCEEDED"])
    with pytest.raises(ValueError, match="plain hex"):
        merge_batch(athena, "abc'; DROP TABLE x --", *WINDOW, sleep=lambda _: None)
    assert athena.queries == []


def test_it_waits_for_a_running_query():
    athena = FakeAthena(["QUEUED", "RUNNING", "SUCCEEDED"])
    stats = merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    assert athena.polls == 3
    assert stats["scanned_bytes"] == 472090


def test_a_failed_load_raises_with_the_reason():
    athena = FakeAthena(["FAILED"])
    with pytest.raises(BronzeLoadFailed, match="it did not work"):
        merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)


def test_the_merge_skips_values_that_have_not_changed():
    # a daily run that appended 1400 identical rows every morning would make
    # known_at mean "when we last looked" instead of "when this appeared"
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    assert "l.quantity_mw <> s.quantity_mw" in sql
    assert "l.source_updated_at IS DISTINCT FROM s.source_updated_at" in sql
    # a period we have never seen has no latest row to compare against
    assert "l.valid_time IS NULL" in sql


def test_only_the_newest_version_is_compared_against():
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    sql = athena.queries[0]
    assert "ORDER BY known_at DESC" in sql
    assert "l.recency = 1" in sql


def test_the_lookback_is_bounded_to_the_same_window():
    # the latest subquery reads bronze, so without this it reads all of it
    athena = FakeAthena(["SUCCEEDED"])
    merge_batch(athena, "abc123", *WINDOW, sleep=lambda _: None)

    assert athena.queries[0].count("TIMESTAMP '2026-08-03 22:00:00.000000'") == 2
