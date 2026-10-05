from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import boto3

from gridlens.contracts.reference import BiddingZone
from gridlens.ingest import redaction
from gridlens.ingest.entsoe import EntsoeClient
from gridlens.lake.backfill import backfill, parse_known_at
from gridlens.lake.staging import StagingArea

log = logging.getLogger("gridlens")
log.setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_token: str | None = None


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set")
    return value


def _entsoe_token() -> str:
    global _token
    if _token is None:
        _token = boto3.client("ssm").get_parameter(
            Name=_env("GRIDLENS_ENTSOE_TOKEN_PARAM", "/gridlens/entsoe/token"),
            WithDecryption=True,
        )["Parameter"]["Value"]
    return _token


def window(
    event: dict[str, Any], known_at: datetime, zone_tz: ZoneInfo
) -> tuple[date, date]:
    """The settlement days to fetch: given outright, or counted back.

    refetch_days is what the daily schedule sends. It counts back from the day
    before yesterday, because yesterday belongs to the 06:30 ingest. The anchor
    is known_at, the scheduled time, not the clock, so a retry an hour later
    fetches the same days rather than a window shifted by one.
    """
    if "start_date" in event and "end_date" in event:
        return (
            date.fromisoformat(str(event["start_date"])),
            date.fromisoformat(str(event["end_date"])),
        )
    if "refetch_days" in event:
        count = int(event["refetch_days"])
        if count < 1:
            raise ValueError("refetch_days has to be at least 1")
        newest = known_at.astimezone(zone_tz).date() - timedelta(days=2)
        return newest - timedelta(days=count - 1), newest
    raise ValueError("pass start_date and end_date, or refetch_days")


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    zone_tz = ZoneInfo(_env("GRIDLENS_ZONE_TZ", "Europe/Paris"))
    known_at = parse_known_at(str(event.get("known_at", "")))
    start, end = window(event, known_at, zone_tz)

    token = _entsoe_token()
    redaction.install(token)

    s3 = boto3.client("s3")
    with EntsoeClient(token) as client:
        summary = backfill(
            client,
            StagingArea(_env("GRIDLENS_RAW_BUCKET"), s3),
            boto3.client("athena"),
            start=start,
            end=end,
            known_at=known_at,
            zone=BiddingZone[_env("GRIDLENS_ZONE", "FR")],
            zone_tz=zone_tz,
            database=_env("GRIDLENS_BRONZE_DATABASE", "gridlens_bronze"),
            workgroup=_env("GRIDLENS_ATHENA_WORKGROUP", "gridlens"),
        )

    log.info("backfill done %s", summary)
    return summary
