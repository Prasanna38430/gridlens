from __future__ import annotations

import json
import struct
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gridlens.contracts.records import GenerationRecord
from gridlens.ingest.entsoe import EntsoeNoData, FetchResult
from gridlens.stream import serialization, topics
from gridlens.stream.producer import DeliveryFailed, days_to_poll, poll_once

FIXTURE = Path(__file__).parent / "fixtures" / "entsoe" / "a75_fr_20260804.xml"

RECORD = GenerationRecord(
    source="entsoe",
    source_document_id="doc-1",
    zone="10YFR-RTE------C",
    production_type="B11",
    direction="generation",
    unit="MW",
    resolution_minutes=15,
    valid_time=datetime(2026, 8, 4, 18, 30, tzinfo=UTC),
    known_at=datetime(2026, 8, 22, 9, 24, 2, tzinfo=UTC),
    quantity_mw=Decimal("2881.92"),
)


# serialization


def test_a_record_survives_the_round_trip_exactly():
    schema_id, back = serialization.decode(serialization.encode(RECORD, 7))
    assert schema_id == 7
    assert back["valid_time"] == RECORD.valid_time
    assert back["known_at"] == RECORD.known_at
    # a decimal, at the schema's scale, and not a float that happens to print
    # the same. the same lesson as gold: no floating point in a figure.
    assert back["quantity_mw"] == Decimal("2881.920")
    assert isinstance(back["quantity_mw"], Decimal)
    assert back["source_updated_at"] is None


def test_the_header_is_a_zero_byte_and_a_big_endian_schema_id():
    payload = serialization.encode(RECORD, 258)
    assert payload[:5] == b"\x00\x00\x00\x01\x02"


@pytest.mark.parametrize(
    "payload", [b"\x00\x00\x01", struct.pack(">bI", 1, 7) + b"\x00"]
)
def test_a_payload_that_is_not_the_wire_format_is_refused(payload: bytes):
    with pytest.raises(serialization.WireFormatError):
        serialization.decode(payload)


def test_one_series_is_one_key_whatever_the_period():
    later = RECORD.model_copy(
        update={"valid_time": datetime(2026, 8, 4, 19, tzinfo=UTC)}
    )
    assert serialization.key(RECORD) == serialization.key(later)
    assert serialization.key(RECORD) == b"entsoe|10YFR-RTE------C|B11|generation"


def test_the_two_directions_of_storage_are_two_series():
    consumption = RECORD.model_copy(update={"direction": "consumption"})
    assert serialization.key(RECORD) != serialization.key(consumption)


def test_the_schema_has_every_field_the_contract_has():
    # a field added to the contract and not to the schema would vanish in the
    # stream without anything failing
    fields = {f["name"] for f in json.loads(serialization.SCHEMA_TEXT)["fields"]}
    assert fields == set(GenerationRecord.model_fields)


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.body


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("put", url, kwargs["json"]))
        return FakeResponse({})

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("post", url, kwargs["json"]))
        return FakeResponse({"id": 3})


def test_registering_sets_backward_compatibility_before_the_schema():
    http = FakeHttp()
    assert serialization.register(http, "http://registry", "t-value") == 3
    assert http.calls[0] == (
        "put",
        "http://registry/config/t-value",
        {"compatibility": "BACKWARD"},
    )
    assert http.calls[1][1] == "http://registry/subjects/t-value/versions"


# topics


def test_observations_are_kept_every_one_not_compacted():
    assert topics.OBSERVATIONS.config["cleanup.policy"] == "delete"
    assert topics.OBSERVATIONS.partitions == 3


class FakeError:
    def __init__(self, code: int) -> None:
        self._code = code

    def code(self) -> int:
        return self._code


class FakeFuture:
    def __init__(self, error: int | None) -> None:
        self.error = error

    def result(self) -> None:
        if self.error is not None:
            raise RuntimeError(FakeError(self.error))


class FakeAdmin:
    def __init__(self, errors: dict[str, int | None]) -> None:
        self.errors = errors
        self.requested: list[Any] = []

    def create_topics(self, new_topics: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.requested = new_topics
        return {t["name"]: FakeFuture(self.errors.get(t["name"])) for t in new_topics}


def new_topic(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"name": name, **kwargs}


def test_missing_topics_are_created_and_existing_ones_left_alone():
    admin = FakeAdmin({topics.QUARANTINE.name: topics.ALREADY_EXISTS})
    assert topics.ensure(admin, new_topic) == [topics.OBSERVATIONS.name]
    assert admin.requested[0]["replication_factor"] == 1


def test_any_other_failure_to_create_a_topic_is_raised():
    admin = FakeAdmin({topics.OBSERVATIONS.name: 29})  # authorisation failed
    with pytest.raises(RuntimeError):
        topics.ensure(admin, new_topic)


# the poll


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 02:59 in Paris: yesterday's last periods may still be arriving
        (
            datetime(2026, 10, 6, 0, 59, tzinfo=UTC),
            [date(2026, 10, 5), date(2026, 10, 6)],
        ),
        (datetime(2026, 10, 6, 1, 0, tzinfo=UTC), [date(2026, 10, 6)]),
        # 00:30 Paris on the 6th is still the 5th in utc
        (
            datetime(2026, 10, 5, 22, 30, tzinfo=UTC),
            [date(2026, 10, 5), date(2026, 10, 6)],
        ),
    ],
)
def test_the_days_polled_follow_the_paris_clock(now: datetime, expected: list[date]):
    assert days_to_poll(now) == expected


class FakeSource:
    def __init__(self, fetched_at: datetime, empty: bool = False) -> None:
        self.fetched_at = fetched_at
        self.empty = empty
        self.windows: list[tuple[datetime, datetime]] = []

    def actual_generation_per_type(
        self, zone: str, start: datetime, end: datetime
    ) -> FetchResult:
        self.windows.append((start, end))
        if self.empty:
            raise EntsoeNoData("999", "no matching data", 200)
        return FetchResult(FIXTURE.read_bytes(), self.fetched_at, 200)


class FakeProducer:
    def __init__(self, refuse: bool = False, stuck: int = 0) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.refuse = refuse
        self.stuck = stuck

    def produce(self, topic: str, **kwargs: Any) -> None:
        self.sent.append((topic, kwargs))

    def poll(self, timeout: float) -> int:
        return 0

    def flush(self, timeout: float) -> int:
        for _, kwargs in self.sent:
            kwargs["on_delivery"]("broker said no" if self.refuse else None, None)
        return self.stuck


def sent_to(producer: FakeProducer, topic: topics.Topic) -> list[dict[str, Any]]:
    return [kwargs for name, kwargs in producer.sent if name == topic.name]


AFTER_THE_DAY = datetime(2026, 8, 5, 4, 30, tzinfo=UTC)
NOON = datetime(2026, 8, 4, 12, 0, 30, tzinfo=UTC)


def test_every_record_fetched_is_published_keyed_by_series():
    producer = FakeProducer()
    summary = poll_once(FakeSource(AFTER_THE_DAY), producer, schema_id=1, now=NOON)
    published = sent_to(producer, topics.OBSERVATIONS)
    assert summary["published"] == len(published) > 1000
    assert len({p["key"] for p in published}) == 15
    _, first = serialization.decode(published[0]["value"])
    assert first["known_at"] == AFTER_THE_DAY


def test_known_at_is_the_fetch_to_the_second():
    producer = FakeProducer()
    fetched = datetime(2026, 8, 5, 4, 30, 12, 345678, tzinfo=UTC)
    poll_once(FakeSource(fetched), producer, schema_id=1, now=NOON)
    _, first = serialization.decode(sent_to(producer, topics.OBSERVATIONS)[0]["value"])
    assert first["known_at"] == datetime(2026, 8, 5, 4, 30, 12, tzinfo=UTC)


def test_a_period_later_than_the_fetch_is_quarantined_not_dropped():
    # fetched at noon, so every period after noon claims to be known before it
    # happened, and the contract refuses it
    producer = FakeProducer()
    summary = poll_once(FakeSource(NOON), producer, schema_id=1, now=NOON)
    refused = sent_to(producer, topics.QUARANTINE)
    assert summary["quarantined"] == len(refused) > 0
    assert summary["published"] > 0
    assert json.loads(refused[0]["value"])["seen_at"] == "2026-08-04T12:00:30+00:00"


def test_a_day_with_nothing_published_is_reported_and_nothing_sent():
    producer = FakeProducer()
    summary = poll_once(FakeSource(NOON, empty=True), producer, schema_id=1, now=NOON)
    assert summary["empty_days"] == ["2026-08-04"]
    assert producer.sent == []


@pytest.mark.parametrize("producer", [FakeProducer(refuse=True), FakeProducer(stuck=4)])
def test_a_poll_that_did_not_reach_the_broker_fails(producer: FakeProducer):
    with pytest.raises(DeliveryFailed):
        poll_once(FakeSource(AFTER_THE_DAY), producer, schema_id=1, now=NOON)
