from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

# entsoe uses a small subset of iso-8601 durations. P1Y is in the spec and is
# deliberately not handled, because a year is not a fixed length and nothing
# here should pretend otherwise.
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


class AmbiguousLocalTime(ValueError):
    """A wall clock reading that happens twice, or never."""


def parse_resolution(value: str) -> timedelta:
    match = _DURATION.match(value)
    if not match:
        raise ValueError(f"unsupported resolution: {value!r}")
    days, hours, minutes = (int(group or 0) for group in match.groups())
    total = timedelta(days=days, hours=hours, minutes=minutes)
    if total <= timedelta(0):
        raise ValueError(f"unsupported resolution: {value!r}")
    return total


def assert_unambiguous(moment: datetime) -> None:
    """Refuse a local time that DST makes either doubled or missing."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime")

    # order matters. both transitions make fold=0 and fold=1 disagree about the
    # offset, so the doubled-hour check below fires on a skipped hour too. the
    # round trip is what tells them apart, so it goes first.
    #
    # a reading skipped by the spring transition comes back from utc as a
    # different reading. comparing the aware values would compare instants and
    # always agree, so compare the naive parts.
    roundtrip = moment.astimezone(UTC).astimezone(moment.tzinfo)
    if roundtrip.replace(tzinfo=None) != moment.replace(tzinfo=None):
        raise AmbiguousLocalTime(f"{moment.isoformat()} does not exist")

    if moment.utcoffset() != moment.replace(fold=1).utcoffset():
        raise AmbiguousLocalTime(f"{moment.isoformat()} happens twice")


def settlement_day(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The local calendar day as a half-open UTC interval.

    25 hours in October, 23 in March, and that is the point of the function.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)

    # european bidding zones all switch at 01:00 utc, so local midnight is never
    # the transition. this guard is here for the day that assumption stops
    # holding rather than because it fails today.
    assert_unambiguous(start)
    assert_unambiguous(end)

    return start.astimezone(UTC), end.astimezone(UTC)


def period_count(start: datetime, end: datetime, resolution: timedelta) -> int:
    span = end - start
    if span <= timedelta(0):
        raise ValueError("end must be after start")
    count, remainder = divmod(span, resolution)
    if remainder:
        raise ValueError(f"{span} does not divide into whole {resolution} periods")
    return count


def position_time(
    interval_start: datetime, resolution: timedelta, position: int
) -> datetime:
    """Positions are 1-based and are an offset, never an index into a list."""
    if position < 1:
        raise ValueError(f"positions are 1-based, got {position}")
    return interval_start + (position - 1) * resolution
