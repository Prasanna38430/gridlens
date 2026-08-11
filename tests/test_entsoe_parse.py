from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gridlens.ingest.entsoe_parse import parse_generation
from gridlens.timeaxis import settlement_day

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
PARIS = ZoneInfo("Europe/Paris")
FR = "10YFR-RTE------C"


def load(name: str):
    return parse_generation((FIXTURES / name).read_bytes())


def test_rejects_an_acknowledgement():
    with pytest.raises(ValueError, match="GL_MarketDocument"):
        load("ack_no_data.xml")


def test_ordinary_day_shape():
    series = load("a75_fr_20260804.xml")

    assert len(series) == 15
    assert {s.zone for s in series} == {FR}
    assert {s.unit for s in series} == {"MAW"}
    assert {s.resolution for s in series} == {timedelta(minutes=15)}
    assert all(s.expected_positions == 96 for s in series)


def test_storage_appears_once_in_each_direction():
    # B10 pumped hydro and B25 both show up twice, once as generation and once
    # as consumption. summing by production type alone double counts them.
    series = load("a75_fr_20260804.xml")
    by_direction: dict[str, set[str]] = {"generation": set(), "consumption": set()}
    for s in series:
        by_direction[s.direction].add(s.production_type)

    assert {"B10", "B25"} <= by_direction["generation"]
    assert by_direction["consumption"] == {"B05", "B10", "B25"}
    assert len(series) > len({s.production_type for s in series})


def test_the_dst_day_carries_100_quarter_hours():
    series = load("a75_fr_20251026_dst.xml")
    first = series[0]

    assert first.interval_start == datetime(2025, 10, 25, 22, tzinfo=UTC)
    assert first.interval_end == datetime(2025, 10, 26, 23, tzinfo=UTC)
    assert (first.interval_end - first.interval_start) == timedelta(hours=25)
    assert all(s.expected_positions == 100 for s in series)


def test_the_request_window_matches_what_the_api_returned():
    # settlement_day is what builds the request. if it disagrees with the
    # timeInterval that came back, every position is offset.
    start, end = settlement_day(date(2025, 10, 26), PARIS)
    first = load("a75_fr_20251026_dst.xml")[0]

    assert (start, end) == (first.interval_start, first.interval_end)


def test_gaps_are_recorded_and_do_not_shift_the_points_after_them():
    # B01 on the dst day has 92 points spread across 100 positions, with holes
    # scattered through the middle rather than trimmed off the end. zipping
    # points against a generated timestamp range would move position 29's
    # value onto position 26's timestamp and every later value with it.
    biomass = next(
        s
        for s in load("a75_fr_20251026_dst.xml")
        if s.production_type == "B01" and s.direction == "generation"
    )

    assert len(biomass.observations) == 92
    assert biomass.missing_positions == (25, 29, 34, 39, 43, 63, 82, 97)

    at_30 = next(o for o in biomass.observations if o.position == 30)
    assert at_30.valid_time == biomass.interval_start + timedelta(minutes=29 * 15)

    # position 30 is the 28th point in the list, because 25 and 29 are absent.
    # a naive zip would put it half an hour early, and every point after it too.
    index = [o.position for o in biomass.observations].index(30)
    assert index == 27
    naive = biomass.interval_start + timedelta(minutes=index * 15)
    assert at_30.valid_time - naive == timedelta(minutes=30)


def test_every_observation_lands_inside_the_declared_interval():
    for series in load("a75_fr_20251026_dst.xml"):
        for observation in series.observations:
            assert series.interval_start <= observation.valid_time
            assert observation.valid_time < series.interval_end


def test_quantities_are_decimal():
    first = load("a75_fr_20260804.xml")[0]
    assert isinstance(first.observations[0].quantity, Decimal)
    # the value that float would round. 288.18 is not representable in binary.
    assert Decimal("288.18") + Decimal("0.02") == Decimal("288.20")


def test_the_local_day_boundary_is_not_the_utc_day_boundary():
    # the dst fixture was requested for the paris day, so it opens at 22:00 utc
    # on the previous date. reading the date part of valid_time as "the trading
    # day" is wrong for the first two hours of every one of them.
    first = load("a75_fr_20251026_dst.xml")[0]
    assert first.interval_start.date() == date(2025, 10, 25)
    assert first.interval_start.astimezone(PARIS).date() == date(2025, 10, 26)


def test_the_two_fixtures_were_requested_on_different_bases():
    # worth pinning so nobody assumes both cover a local day. the august one
    # was requested with utc midnights, which is a 24 hour window that happens
    # to be offset two hours from the french trading day.
    august = load("a75_fr_20260804.xml")[0]
    assert august.interval_start == datetime(2026, 8, 4, tzinfo=UTC)
    assert august.interval_start.astimezone(PARIS).hour == 2
