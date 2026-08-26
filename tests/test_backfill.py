from __future__ import annotations

import random
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gridlens.ingest.entsoe import EntsoeClient
from gridlens.ingest.ratelimit import TokenBucket
from gridlens.lake.backfill import MAX_DAYS, backfill, days, parse_known_at
from gridlens.lake.staging import StagingArea

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
KNOWN_AT = datetime(2026, 8, 25, 4, 30, tzinfo=UTC)
PARIS = ZoneInfo("Europe/Paris")


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.puts.append(kwargs)
        return {}


class FakeAthena:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.queries.append(kwargs["QueryString"])
        return {"QueryExecutionId": f"q-{len(self.queries)}"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {
                    "DataScannedInBytes": 1000,
                    "TotalExecutionTimeInMillis": 10,
                },
            }
        }


def entsoe(
    body: bytes | None = None, status: int = 200
) -> tuple[EntsoeClient, list[httpx.Request]]:
    payload = (
        body if body is not None else (FIXTURES / "a75_fr_20260804.xml").read_bytes()
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=payload)

    client = EntsoeClient(
        "11111111-2222-3333-4444-555555555555",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket=TokenBucket(1000.0, 10_000, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=lambda _: None,
        rng=random.Random(0),
    )
    return client, calls


def run(start: date, end: date, known_at: datetime = KNOWN_AT, **kwargs: Any):
    client, calls = entsoe(**kwargs)
    s3, athena = FakeS3(), FakeAthena()
    summary = backfill(
        client,
        StagingArea("raw-bucket", s3),
        athena,
        start=start,
        end=end,
        known_at=known_at,
    )
    return summary, calls, s3, athena


def test_days_is_inclusive_at_both_ends():
    assert list(days(date(2026, 8, 1), date(2026, 8, 3))) == [
        date(2026, 8, 1),
        date(2026, 8, 2),
        date(2026, 8, 3),
    ]


def test_one_fetch_one_stage_and_one_merge_per_day():
    summary, calls, s3, athena = run(date(2026, 8, 1), date(2026, 8, 3))

    assert len(calls) == 3
    assert len(s3.puts) == 3
    assert len(athena.queries) == 3
    assert summary["days_merged"] == 3
    assert summary["rows"] == 3 * 1388


def test_every_row_in_the_run_shares_one_known_at():
    # the run learned all of it at one moment. a per day clock reading would
    # make three days look like three separate revisions.
    _, _, s3, _ = run(date(2026, 8, 1), date(2026, 8, 3))

    stamps = {
        line.split(b'"known_at": "')[1].split(b'"')[0]
        for put in s3.puts
        for line in put["Body"].splitlines()
    }
    assert stamps == {b"2026-08-25 04:30:00.000000"}


def test_each_merge_is_bounded_to_its_own_day():
    _, _, _, athena = run(date(2026, 8, 1), date(2026, 8, 2))

    assert "t.valid_time >= TIMESTAMP '2026-07-31 22:00:00.000000'" in athena.queries[0]
    assert "t.valid_time >= TIMESTAMP '2026-08-01 22:00:00.000000'" in athena.queries[1]


def test_a_window_longer_than_the_limit_is_refused_before_any_call():
    client, calls = entsoe()
    s3, athena = FakeS3(), FakeAthena()

    with pytest.raises(ValueError, match=f"{MAX_DAYS} day limit"):
        backfill(
            client,
            StagingArea("raw-bucket", s3),
            athena,
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            known_at=KNOWN_AT,
        )
    assert calls == []
    assert s3.puts == []


def test_a_backwards_range_is_refused():
    client, _ = entsoe()
    with pytest.raises(ValueError, match="before start"):
        backfill(
            client,
            StagingArea("b", FakeS3()),
            FakeAthena(),
            start=date(2026, 8, 5),
            end=date(2026, 8, 1),
            known_at=KNOWN_AT,
        )


def test_a_naive_known_at_is_refused():
    client, _ = entsoe()
    with pytest.raises(ValueError, match="utc aware"):
        backfill(
            client,
            StagingArea("b", FakeS3()),
            FakeAthena(),
            start=date(2026, 8, 1),
            end=date(2026, 8, 1),
            known_at=datetime(2026, 8, 25, 4, 30),
        )


def test_a_day_with_no_data_is_reported_and_skipped():
    body = (FIXTURES / "ack_no_data.xml").read_bytes()
    summary, _, s3, athena = run(date(2026, 8, 1), date(2026, 8, 2), body=body)

    assert summary["days_merged"] == 0
    assert summary["days_empty"] == ["2026-08-01", "2026-08-02"]
    assert athena.queries == []
    assert s3.puts == []


def test_known_at_comes_from_the_event_when_the_scheduler_supplies_it():
    # this is the whole idempotence mechanism. eventbridge substitutes its
    # scheduled time, and that value is identical across retries.
    assert parse_known_at("2026-08-25T04:30:00Z") == KNOWN_AT
    assert parse_known_at("2026-08-25T06:30:00+02:00") == KNOWN_AT


def test_known_at_falls_back_to_the_clock_only_when_absent():
    fixed = datetime(2030, 1, 1, tzinfo=UTC)
    assert parse_known_at("", lambda: fixed) == fixed


def test_a_known_at_without_an_offset_is_refused():
    with pytest.raises(ValueError, match="no offset"):
        parse_known_at("2026-08-25T04:30:00")


def test_the_dst_day_is_fetched_as_25_hours():
    _, calls, _, _ = run(date(2026, 10, 25), date(2026, 10, 25))

    params = calls[0].url.params
    assert params["periodStart"] == "202610242200"
    assert params["periodEnd"] == "202610252300"
