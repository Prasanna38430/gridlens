from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gridlens.contracts.gate import apply_gate
from gridlens.contracts.reference import BiddingZone
from gridlens.ingest.entsoe import EntsoeClient, EntsoeNoData
from gridlens.ingest.entsoe_parse import parse_generation
from gridlens.lake.bronze import merge_batch
from gridlens.lake.staging import StagingArea
from gridlens.timeaxis import settlement_day

log = logging.getLogger("gridlens")

# A day costs roughly nine seconds end to end and lambda stops at fifteen
# minutes. Thirty one keeps a whole calendar month inside one invocation with
# room to spare, and anything longer is the orchestrator's job to chunk rather
# than something to discover halfway through.
MAX_DAYS = 31

PARIS = ZoneInfo("Europe/Paris")


def days(start: date, end: date) -> Iterator[date]:
    """Every date from start to end inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def backfill(
    client: EntsoeClient,
    staging: StagingArea,
    query_engine: Any,
    *,
    start: date,
    end: date,
    known_at: datetime,
    zone: BiddingZone = BiddingZone.FR,
    zone_tz: ZoneInfo = PARIS,
    database: str = "gridlens_bronze",
    workgroup: str = "gridlens",
) -> dict[str, Any]:
    """Re-fetch a range of settlement days and merge it into bronze once.

    known_at is an argument rather than a clock reading, and that is the whole
    design. A retry that called datetime.now() would stamp a different
    known_at, find nothing to match on, and insert the range a second time as
    if the source had revised every value.
    """
    if end < start:
        raise ValueError("end is before start")
    span = (end - start).days + 1
    if span > MAX_DAYS:
        raise ValueError(f"{span} days is more than the {MAX_DAYS} day limit")
    if known_at.tzinfo is None or known_at.utcoffset() != timedelta(0):
        raise ValueError("known_at must be utc aware")

    accepted = 0
    merged_days = 0
    empty_days: list[str] = []
    scanned = 0

    for day in days(start, end):
        window_start, window_end = settlement_day(day, zone_tz)
        try:
            fetched = client.actual_generation_per_type(
                zone.value, window_start, window_end
            )
        except EntsoeNoData:
            # a day the platform has nothing for. not a failure, and not
            # something to retry, so it is reported and skipped.
            empty_days.append(day.isoformat())
            continue

        result = apply_gate(parse_generation(fetched.body), known_at)
        batch_id, staged = staging.write(result.records)
        if not staged:
            empty_days.append(day.isoformat())
            continue

        stats = merge_batch(
            query_engine,
            batch_id,
            window_start,
            window_end,
            database=database,
            workgroup=workgroup,
        )
        accepted += staged
        merged_days += 1
        scanned += int(stats["scanned_bytes"])
        log.info("backfilled %s: %s rows", day.isoformat(), staged)

    return {
        "zone": zone.name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "known_at": known_at.isoformat(),
        "days_merged": merged_days,
        "days_empty": empty_days,
        "rows": accepted,
        "scanned_bytes": scanned,
    }


def parse_known_at(
    value: str, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> datetime:
    """Read known_at from an event, falling back to now only when absent.

    EventBridge Scheduler substitutes its scheduled time into the payload, and
    that value is identical across retries of the same firing. Reading the
    clock instead is what turns a retry into a phantom revision.
    """
    if not value:
        return now()
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"known_at has no offset: {value!r}")
    return moment.astimezone(UTC)
