from __future__ import annotations

import logging
import os
from datetime import date
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


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    if "start_date" not in event or "end_date" not in event:
        raise ValueError("start_date and end_date are required")

    token = _entsoe_token()
    redaction.install(token)

    s3 = boto3.client("s3")
    with EntsoeClient(token) as client:
        summary = backfill(
            client,
            StagingArea(_env("GRIDLENS_RAW_BUCKET"), s3),
            boto3.client("athena"),
            start=date.fromisoformat(str(event["start_date"])),
            end=date.fromisoformat(str(event["end_date"])),
            known_at=parse_known_at(str(event.get("known_at", ""))),
            zone=BiddingZone[_env("GRIDLENS_ZONE", "FR")],
            zone_tz=ZoneInfo(_env("GRIDLENS_ZONE_TZ", "Europe/Paris")),
            database=_env("GRIDLENS_BRONZE_DATABASE", "gridlens_bronze"),
            workgroup=_env("GRIDLENS_ATHENA_WORKGROUP", "gridlens"),
        )

    log.info("backfill done %s", summary)
    return summary
