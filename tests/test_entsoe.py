from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from gridlens.ingest.entsoe import (
    EntsoeAuthError,
    EntsoeClient,
    EntsoeNoData,
    EntsoeUnavailable,
    RateLimited,
    format_period,
)
from gridlens.ingest.ratelimit import TokenBucket

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
TOKEN = "11111111-2222-3333-4444-555555555555"
FR = "10YFR-RTE------C"
PARIS = ZoneInfo("Europe/Paris")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def build(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> EntsoeClient:
    return EntsoeClient(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        # a bucket that never makes the tests wait
        bucket=TokenBucket(1000.0, 10_000, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=lambda _: None,
        rng=random.Random(0),
        **kwargs,
    )


def always(status: int, body: bytes, headers: dict[str, str] | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body, headers=headers)

    return handler, calls


def test_returns_the_body_on_success():
    handler, calls = always(200, fixture("a75_fr_20260804.xml"))
    result = build(handler).actual_generation_per_type(
        FR, datetime(2026, 8, 4, tzinfo=UTC), datetime(2026, 8, 5, tzinfo=UTC)
    )

    assert result.status_code == 200
    assert b"GL_MarketDocument" in result.body
    assert len(calls) == 1


def test_sends_the_token_and_the_period():
    handler, calls = always(200, fixture("a75_fr_20260804.xml"))
    build(handler).actual_generation_per_type(
        FR, datetime(2026, 8, 4, tzinfo=UTC), datetime(2026, 8, 5, tzinfo=UTC)
    )

    params = calls[0].url.params
    assert params["securityToken"] == TOKEN
    assert params["documentType"] == "A75"
    assert params["periodStart"] == "202608040000"
    assert params["periodEnd"] == "202608050000"


def test_fetched_at_is_aware_and_utc():
    handler, _ = always(200, fixture("a75_fr_20260804.xml"))
    result = build(handler).fetch({"documentType": "A75"})

    assert result.fetched_at.tzinfo is not None
    assert result.fetched_at.utcoffset() == UTC.utcoffset(None)


def test_no_data_acknowledgement_raises_even_on_http_200():
    # this is the one that matters. entsoe returns "no matching data" with a
    # 200, so a status-only check would pass an empty payload downstream.
    handler, _ = always(200, fixture("ack_no_data.xml"))

    with pytest.raises(EntsoeNoData) as caught:
        build(handler).fetch({"documentType": "A75"})

    assert caught.value.code == "999"
    assert caught.value.status_code == 200


def test_bad_token_raises_an_auth_error():
    handler, _ = always(401, fixture("ack_bad_token.xml"))

    with pytest.raises(EntsoeAuthError) as caught:
        build(handler).fetch({"documentType": "A75"})

    assert "Authentication failed" in caught.value.text


def test_a_typo_in_the_zone_is_indistinguishable_from_an_empty_window():
    # recorded against a zone code that does not exist. entsoe answers with the
    # same 999 "no matching data" it uses for a genuinely empty period, so the
    # client cannot tell them apart and neither can anything above it.
    handler, _ = always(200, fixture("ack_bad_domain.xml"))

    with pytest.raises(EntsoeNoData):
        build(handler).fetch({"documentType": "A75", "in_Domain": "10YNOTAREALZONE1"})


def test_rate_limit_raises_without_retrying():
    handler, calls = always(429, b"slow down")

    with pytest.raises(RateLimited) as caught:
        build(handler).fetch({"documentType": "A75"})

    assert len(calls) == 1
    assert caught.value.retry_after == 600.0


def test_rate_limit_honours_retry_after():
    handler, _ = always(429, b"slow down", {"Retry-After": "120"})

    with pytest.raises(RateLimited) as caught:
        build(handler).fetch({"documentType": "A75"})

    assert caught.value.retry_after == 120.0


def test_retries_a_5xx_then_succeeds():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, content=b"upstream busy")
        return httpx.Response(200, content=fixture("a75_fr_20260804.xml"))

    result = build(handler).fetch({"documentType": "A75"})

    assert len(calls) == 3
    assert result.status_code == 200


def test_gives_up_after_the_attempt_budget():
    handler, calls = always(503, b"upstream busy")

    with pytest.raises(EntsoeUnavailable):
        build(handler, max_attempts=3).fetch({"documentType": "A75"})

    assert len(calls) == 3


def test_retries_transport_errors():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(200, content=fixture("a75_fr_20260804.xml"))

    assert build(handler).fetch({"documentType": "A75"}).status_code == 200
    assert len(calls) == 2


def test_the_token_never_appears_in_our_error_messages():
    handler, _ = always(503, b"upstream busy")

    with pytest.raises(EntsoeUnavailable) as caught:
        build(handler, max_attempts=2).fetch({"documentType": "A75"})

    assert TOKEN not in str(caught.value)


def test_repr_redacts_the_token():
    handler, _ = always(200, b"")
    assert TOKEN not in repr(build(handler))


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="naive"):
        format_period(datetime(2026, 8, 4, 12, 0))


def test_period_is_converted_to_utc_with_the_right_seasonal_offset():
    # paris is utc+2 in august and utc+1 in january. both must land on midnight
    # utc, which is the whole reason naive datetimes are refused.
    assert format_period(datetime(2026, 8, 4, 2, 0, tzinfo=PARIS)) == "202608040000"
    assert format_period(datetime(2026, 1, 15, 1, 0, tzinfo=PARIS)) == "202601150000"
