from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gridlens.contracts.reference import ProductionType
from gridlens.ingest.errors import RateLimited
from gridlens.ingest.ratelimit import TokenBucket
from gridlens.ingest.rte import (
    RteAuthError,
    RteClient,
    RteNoApplication,
    RteUnavailable,
)
from gridlens.ingest.rte_parse import parse_actual_generation, total_matches_components

FIXTURES = Path(__file__).parent / "fixtures" / "rte"
PARIS = ZoneInfo("Europe/Paris")
KNOWN_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
DST_DAY = "actual_generation_fr_20251026_dst.json"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def build(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> tuple[RteClient, Clock]:
    clock = Clock()
    client = RteClient(
        "id",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket=TokenBucket(1000.0, 10_000, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=lambda _: None,
        now=clock,
        rng=random.Random(0),
        **kwargs,
    )
    return client, clock


def routed(
    data_status: int = 200,
    data_body: bytes = b"{}",
    headers: dict[str, str] | None = None,
    expires_in: int = 3600,
):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.startswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "token_type": "Bearer",
                    "expires_in": expires_in,
                },
            )
        return httpx.Response(data_status, content=data_body, headers=headers)

    return handler, calls


def test_exchanges_credentials_then_calls_with_a_bearer():
    handler, calls = routed(data_body=fixture(DST_DAY))
    client, _ = build(handler)

    client.actual_generation_per_type(
        datetime(2025, 10, 26, tzinfo=PARIS), datetime(2025, 10, 27, tzinfo=PARIS)
    )

    token_call, data_call = calls
    assert token_call.url.path == "/token/oauth/"
    assert token_call.headers["Authorization"].startswith("Basic ")
    assert data_call.headers["Authorization"] == "Bearer tok"
    assert data_call.url.params["start_date"] == "2025-10-25T22:00:00+00:00"


def test_the_token_is_reused_until_it_nearly_expires():
    handler, _ = routed(data_body=fixture(DST_DAY), expires_in=3600)
    client, clock = build(handler)
    window = (
        datetime(2025, 10, 26, tzinfo=PARIS),
        datetime(2025, 10, 27, tzinfo=PARIS),
    )

    client.actual_generation_per_type(*window)
    client.actual_generation_per_type(*window)
    assert client.token_fetches == 1

    # inside the 60 second skew, so it refreshes early rather than mid-flight
    clock.now += timedelta(seconds=3541)
    client.actual_generation_per_type(*window)
    assert client.token_fetches == 2


def test_a_401_triggers_exactly_one_forced_refresh():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.startswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(401, content=b"expired")

    client, _ = build(handler)
    with pytest.raises(RteAuthError):
        client.actual_generation_per_type(
            datetime(2025, 10, 26, tzinfo=PARIS), datetime(2025, 10, 27, tzinfo=PARIS)
        )

    assert seen.count("/token/oauth/") == 2


def test_403_says_the_application_is_missing():
    handler, _ = routed(data_status=403)
    client, _ = build(handler)

    with pytest.raises(RteNoApplication, match="no application"):
        client.actual_generation_per_type(
            datetime(2025, 10, 26, tzinfo=PARIS), datetime(2025, 10, 27, tzinfo=PARIS)
        )


def test_429_uses_the_header_rte_actually_sends():
    handler, _ = routed(data_status=429, headers={"Retry-After": "90"})
    client, _ = build(handler)

    with pytest.raises(RateLimited) as caught:
        client.actual_generation_per_type(
            datetime(2025, 10, 26, tzinfo=PARIS), datetime(2025, 10, 27, tzinfo=PARIS)
        )
    assert caught.value.retry_after == 90.0


def test_gives_up_on_repeated_5xx():
    handler, calls = routed(data_status=503)
    client, _ = build(handler, max_attempts=3)

    with pytest.raises(RteUnavailable):
        client.actual_generation_per_type(
            datetime(2025, 10, 26, tzinfo=PARIS), datetime(2025, 10, 27, tzinfo=PARIS)
        )
    assert sum(1 for c in calls if not c.url.path.startswith("/token")) == 3


def test_the_window_limit_is_enforced_before_the_call():
    handler, calls = routed()
    client, _ = build(handler)

    with pytest.raises(ValueError, match="155 day"):
        client.actual_generation_per_type(
            datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)
        )
    assert calls == []


def test_naive_datetimes_are_refused():
    handler, _ = routed()
    client, _ = build(handler)

    with pytest.raises(ValueError, match="naive"):
        client.actual_generation_per_type(
            datetime(2025, 10, 26), datetime(2025, 10, 27, tzinfo=UTC)
        )


def test_credentials_never_appear_in_the_repr():
    handler, _ = routed()
    client, _ = build(handler)
    assert "secret" not in repr(client)


def test_the_dropped_hour_becomes_a_gap():
    # RTE publishes 24 hourly values across a 25 hour window and marks nothing.
    # the only way to see it is to build the grid the window implies.
    result = parse_actual_generation(fixture(DST_DAY), KNOWN_AT)
    nuclear_gaps = [
        g for g in result.gaps if g.production_type == ProductionType.NUCLEAR.value
    ]

    assert len(nuclear_gaps) == 1
    assert nuclear_gaps[0].valid_time == datetime(2025, 10, 26, 0, 0, tzinfo=UTC)
    # 12 production types, one missing hour each
    assert len(result.gaps) == 12


def test_entsoe_publishes_the_hour_rte_drops():
    from gridlens.contracts.gate import apply_gate
    from gridlens.ingest.entsoe_parse import parse_generation

    missing = datetime(2025, 10, 26, 0, 0, tzinfo=UTC)
    entsoe = apply_gate(
        parse_generation(
            (
                Path(__file__).parent
                / "fixtures"
                / "entsoe"
                / "a75_fr_20251026_dst.xml"
            ).read_bytes()
        ),
        KNOWN_AT,
    )
    covered = [
        r
        for r in entsoe.records
        if r.production_type is ProductionType.NUCLEAR
        and missing <= r.valid_time < missing + timedelta(hours=1)
    ]

    assert len(covered) == 4  # four quarter hours entsoe has and rte does not


def test_pumping_becomes_consumption_rather_than_a_negative_number():
    result = parse_actual_generation(fixture(DST_DAY), KNOWN_AT)
    pumped = [
        r
        for r in result.records
        if r.production_type is ProductionType.HYDRO_PUMPED_STORAGE
    ]

    assert {r.direction for r in pumped} == {"generation", "consumption"}
    assert all(r.quantity_mw >= 0 for r in pumped)
    assert sum(1 for r in pumped if r.direction == "consumption") == 17


def test_the_total_row_is_excluded_from_records():
    result = parse_actual_generation(fixture(DST_DAY), KNOWN_AT)
    assert len({r.production_type for r in result.records}) == 12
    assert result.violations == ()


def test_the_total_row_is_used_as_an_arithmetic_check():
    assert total_matches_components(fixture(DST_DAY)) == {}


def test_a_truncated_response_is_caught_by_the_total_check():
    payload = json.loads(fixture(DST_DAY))
    key = "actual_generations_per_production_type"
    for entry in payload[key]:
        if entry["production_type"] == "NUCLEAR":
            entry["values"] = entry["values"][:5]

    broken = total_matches_components(json.dumps(payload).encode())
    assert len(broken) == 19


def test_source_updated_at_survives_and_is_not_known_at():
    result = parse_actual_generation(fixture(DST_DAY), KNOWN_AT)
    updated = {r.source_updated_at for r in result.records}

    assert KNOWN_AT not in updated
    assert {r.known_at for r in result.records} == {KNOWN_AT}
    # revised two days after the settlement day it describes
    assert datetime(2025, 10, 28, 0, 11, 27, tzinfo=UTC) in updated


def test_an_unknown_sector_is_quarantined_not_crashed():
    payload = json.loads(fixture(DST_DAY))
    payload["actual_generations_per_production_type"][0]["production_type"] = "FUSION"

    result = parse_actual_generation(json.dumps(payload).encode(), KNOWN_AT)
    assert result.violations[0].reason == "unknown_production_type"
    assert "FUSION" in result.violations[0].detail
    assert result.accepted > 0


def test_the_two_sources_agree_on_the_zone_and_the_unit():
    result = parse_actual_generation(fixture(DST_DAY), KNOWN_AT)
    assert {r.unit for r in result.records} == {"MW"}
    assert {r.source for r in result.records} == {"rte"}
    assert all(r.source_document_id is None for r in result.records)
