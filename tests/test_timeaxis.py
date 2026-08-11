from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gridlens.timeaxis import (
    AmbiguousLocalTime,
    assert_unambiguous,
    parse_resolution,
    period_count,
    position_time,
    settlement_day,
)

PARIS = ZoneInfo("Europe/Paris")
HELSINKI = ZoneInfo("Europe/Helsinki")


def test_parses_the_resolutions_entsoe_actually_sends():
    assert parse_resolution("PT15M") == timedelta(minutes=15)
    assert parse_resolution("PT30M") == timedelta(minutes=30)
    assert parse_resolution("PT60M") == timedelta(hours=1)
    assert parse_resolution("PT1H") == timedelta(hours=1)
    assert parse_resolution("P1D") == timedelta(days=1)
    assert parse_resolution("P7D") == timedelta(days=7)


def test_refuses_resolutions_that_are_not_a_fixed_length():
    # P1Y is in the entsoe spec. a year is not a timedelta and pretending
    # otherwise is how you get an off-by-one-day bug every leap year.
    with pytest.raises(ValueError, match="unsupported"):
        parse_resolution("P1Y")
    with pytest.raises(ValueError, match="unsupported"):
        parse_resolution("PT0M")


def test_an_ordinary_day_is_24_hours():
    start, end = settlement_day(date(2026, 8, 4), PARIS)
    assert (end - start) == timedelta(hours=24)
    assert start == datetime(2026, 8, 3, 22, tzinfo=UTC)


def test_the_october_day_is_25_hours():
    start, end = settlement_day(date(2026, 10, 25), PARIS)
    assert (end - start) == timedelta(hours=25)
    assert period_count(start, end, timedelta(minutes=15)) == 100


def test_the_march_day_is_23_hours():
    start, end = settlement_day(date(2026, 3, 29), PARIS)
    assert (end - start) == timedelta(hours=23)
    assert period_count(start, end, timedelta(minutes=15)) == 92


def test_zones_transition_at_the_same_instant_but_different_local_times():
    # every european bidding zone switches at 01:00 utc. paris is utc+2 going
    # into it and helsinki utc+3, so a single request window covering "the
    # local day" is a different utc window per zone.
    paris_start, _ = settlement_day(date(2026, 10, 25), PARIS)
    helsinki_start, _ = settlement_day(date(2026, 10, 25), HELSINKI)
    assert paris_start == datetime(2026, 10, 24, 22, tzinfo=UTC)
    assert helsinki_start == datetime(2026, 10, 24, 21, tzinfo=UTC)


def test_the_doubled_hour_is_rejected():
    with pytest.raises(AmbiguousLocalTime, match="twice"):
        assert_unambiguous(datetime(2026, 10, 25, 2, 30, tzinfo=PARIS))


def test_the_missing_hour_is_rejected():
    with pytest.raises(AmbiguousLocalTime, match="does not exist"):
        assert_unambiguous(datetime(2026, 3, 29, 2, 30, tzinfo=PARIS))


def test_two_local_times_an_hour_apart_compare_equal():
    # 02:30 on the october sunday happens once at utc+2 and again at utc+1.
    # PEP 495 says two aware datetimes in the *same* zone compare on the wall
    # clock and ignore fold, so python calls these equal and their difference
    # zero, while their utc values are an hour apart. a dict keyed on local
    # time silently keeps one and drops the other.
    first = datetime(2026, 10, 25, 2, 30, tzinfo=PARIS, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=PARIS, fold=1)

    assert first == second
    assert (second - first) == timedelta(0)
    assert len({first, second}) == 1

    # convert to utc and the truth comes back
    assert first.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)


def test_period_count_refuses_a_ragged_interval():
    start = datetime(2026, 8, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="whole"):
        period_count(start, start + timedelta(minutes=70), timedelta(minutes=15))


def test_position_is_an_offset_not_an_index():
    start = datetime(2026, 8, 4, tzinfo=UTC)
    quarter = timedelta(minutes=15)

    assert position_time(start, quarter, 1) == start
    assert position_time(start, quarter, 29) == start + timedelta(minutes=420)

    with pytest.raises(ValueError, match="1-based"):
        position_time(start, quarter, 0)
