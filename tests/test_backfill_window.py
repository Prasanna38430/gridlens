from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from gridlens.handlers.backfill_entsoe import window
from gridlens.lake.backfill import MAX_DAYS

PARIS = ZoneInfo("Europe/Paris")

# 05:30 in Paris on 2026-10-06, which is when the daily re-fetch fires
SCHEDULED = datetime(2026, 10, 6, 3, 30, tzinfo=UTC)


def test_explicit_dates_are_used_as_given():
    event = {"start_date": "2026-09-18", "end_date": "2026-09-18"}
    assert window(event, SCHEDULED, PARIS) == (date(2026, 9, 18), date(2026, 9, 18))


def test_refetch_stops_the_day_before_yesterday():
    # yesterday is the 06:30 ingest's, and it has not run yet at 05:30
    start, end = window({"refetch_days": 27}, SCHEDULED, PARIS)
    assert end == date(2026, 10, 4)
    assert start == date(2026, 9, 8)
    assert (end - start).days + 1 == 27


def test_refetch_counts_from_the_paris_date_not_the_utc_one():
    # 00:30 in Paris on the 6th is still the 5th in utc
    just_after_midnight = datetime(2026, 10, 5, 22, 30, tzinfo=UTC)
    _, end = window({"refetch_days": 1}, just_after_midnight, PARIS)
    assert end == date(2026, 10, 4)


def test_a_retry_with_the_same_known_at_fetches_the_same_days():
    first = window({"refetch_days": 27}, SCHEDULED, PARIS)
    again = window({"refetch_days": 27}, SCHEDULED, PARIS)
    assert first == again


def test_the_scheduled_window_fits_under_the_backfill_limit():
    start, end = window({"refetch_days": 27}, SCHEDULED, PARIS)
    assert (end - start).days + 1 <= MAX_DAYS


@pytest.mark.parametrize("event", [{}, {"start_date": "2026-09-18"}])
def test_an_event_without_a_window_is_refused(event: dict[str, str]):
    with pytest.raises(ValueError, match="refetch_days"):
        window(event, SCHEDULED, PARIS)


def test_a_window_of_nothing_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        window({"refetch_days": 0}, SCHEDULED, PARIS)
