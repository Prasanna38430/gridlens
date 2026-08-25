from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import boto3

from gridlens.contracts.gate import apply_gate
from gridlens.contracts.quarantine import S3Quarantine
from gridlens.contracts.reference import BiddingZone
from gridlens.ingest import redaction
from gridlens.ingest.entsoe import EntsoeClient, EntsoeNoData, RateLimited
from gridlens.ingest.entsoe_parse import parse_generation
from gridlens.lake.backfill import parse_known_at
from gridlens.lake.bronze import merge_batch
from gridlens.lake.staging import StagingArea
from gridlens.timeaxis import settlement_day

log = logging.getLogger("gridlens")
log.setLevel(logging.INFO)

# httpx logs the full request url at INFO, and the entsoe token is a query
# parameter, so a root logger at INFO writes a live credential into cloudwatch
# on every call. this is not paranoia, it happened on the first deploy.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# reused across warm invocations. the token does not change between calls and
# ssm GetParameter is throttled, so re-reading it every time buys nothing.
_token: str | None = None


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set")
    return value


def _entsoe_token() -> str:
    global _token
    if _token is None:
        ssm = boto3.client("ssm")
        _token = ssm.get_parameter(
            Name=_env("GRIDLENS_ENTSOE_TOKEN_PARAM", "/gridlens/entsoe/token"),
            WithDecryption=True,
        )["Parameter"]["Value"]
    return _token


def _target_day(event: dict[str, Any]) -> date:
    """The settlement day to fetch, defaulting to the last complete one."""
    if asked := event.get("valid_date"):
        return date.fromisoformat(str(asked))
    lookback = int(_env("GRIDLENS_LOOKBACK_DAYS", "1"))
    zone_tz = ZoneInfo(_env("GRIDLENS_ZONE_TZ", "Europe/Paris"))
    return (datetime.now(UTC).astimezone(zone_tz) - timedelta(days=lookback)).date()


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    zone = BiddingZone[_env("GRIDLENS_ZONE", "FR")]
    zone_tz = ZoneInfo(_env("GRIDLENS_ZONE_TZ", "Europe/Paris"))
    day = _target_day(event)
    start, end = settlement_day(day, zone_tz)

    token = _entsoe_token()
    # belt and braces. the httpx logger is muted above, and this catches the
    # next library that decides a url is worth logging.
    redaction.install(token)

    s3 = boto3.client("s3")
    log.info(
        "fetching zone=%s valid_date=%s window=%s/%s",
        zone.name,
        day.isoformat(),
        start.isoformat(),
        end.isoformat(),
    )

    with EntsoeClient(token) as client:
        try:
            fetched = client.actual_generation_per_type(zone.value, start, end)
        except EntsoeNoData as exc:
            # a window the platform has nothing for is an outcome, not a fault.
            # it is also what a mistyped zone looks like, which is why the zone
            # is an enum and cannot be mistyped here.
            log.warning("no data for %s: %s", day, exc.text)
            return {"zone": zone.name, "valid_date": day.isoformat(), "no_data": True}
        except RateLimited as exc:
            # the token is banned for minutes. failing now lets the schedule
            # come back rather than burning billed wall time asleep.
            log.error("rate limited, retry after %ss", exc.retry_after)
            raise

    # the scheduler substitutes its scheduled time into the payload, and that
    # value is identical across retries of the same firing. taking the clock
    # here instead is what would turn a retry into a phantom revision.
    known_at = parse_known_at(
        str(event.get("known_at", "")), lambda: fetched.fetched_at
    )
    raw_key = (
        f"entsoe/a75/zone={zone.name}/valid_date={day.isoformat()}"
        f"/{known_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.xml"
    )
    s3.put_object(
        Bucket=_env("GRIDLENS_RAW_BUCKET"),
        Key=raw_key,
        Body=fetched.body,
        ContentType="application/xml",
    )

    result = apply_gate(parse_generation(fetched.body), known_at)
    quarantined = S3Quarantine(
        _env("GRIDLENS_QUARANTINE_BUCKET"), s3, prefix="entsoe"
    ).write(result.violations, known_at)

    # staged as ndjson, then appended by athena. the alternative was pyiceberg,
    # which drags pyarrow and roughly a hundred megabytes into a bundle that is
    # currently nine.
    raw_bucket = _env("GRIDLENS_RAW_BUCKET")
    batch_id, staged = StagingArea(raw_bucket, s3).write(result.records)
    loaded: dict[str, Any] = {}
    if staged:
        loaded = merge_batch(
            boto3.client("athena"),
            batch_id,
            start,
            end,
            database=_env("GRIDLENS_BRONZE_DATABASE", "gridlens_bronze"),
            workgroup=_env("GRIDLENS_ATHENA_WORKGROUP", "gridlens"),
        )

    summary = {
        "zone": zone.name,
        "valid_date": day.isoformat(),
        "known_at": known_at.isoformat(),
        "raw_key": raw_key,
        "raw_bytes": len(fetched.body),
        "accepted": result.accepted,
        "quarantined": quarantined,
        "gaps": len(result.gaps),
        "batch_id": batch_id,
        "loaded_rows": staged,
        "load_scanned_bytes": loaded.get("scanned_bytes", 0),
    }
    log.info("done %s", summary)
    return summary
