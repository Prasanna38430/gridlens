"""Poll ENTSO-E for today's generation and publish what it says, every time.

The daily Lambda learns a settlement day once, the morning after. This learns
it while it happens: ENTSO-E publishes the current day about forty minutes
behind real time, and a poll every fifteen minutes sees each period appear and
sometimes change. Day 24 compares what this stream said with what the morning
fetch settled on.

Every poll publishes everything it fetched, unchanged values included. The
topic is a record of what the source said and when, so a repeated value is a
fact too. Collapsing repeats is the consumer's job, on day 23, where it can be
tested against a duplicate flood rather than assumed away here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from gridlens.contracts.gate import apply_gate
from gridlens.contracts.reference import BiddingZone
from gridlens.ingest.entsoe import EntsoeNoData, FetchResult
from gridlens.ingest.entsoe_parse import parse_generation
from gridlens.stream import serialization, topics
from gridlens.timeaxis import settlement_day

log = logging.getLogger("gridlens.stream")

PARIS = ZoneInfo("Europe/Paris")
POLL_SECONDS = 15 * 60

# The last periods of a day are published after midnight, so the previous day
# stays in the poll until this hour Paris time. Three hours covers the forty
# minute lag several times over without polling a finished day all morning.
YESTERDAY_UNTIL_HOUR = 3


class Source(Protocol):
    def actual_generation_per_type(
        self, zone: str, start: datetime, end: datetime
    ) -> FetchResult: ...


class Producer(Protocol):
    def produce(
        self,
        topic: str,
        *,
        value: bytes | None = ...,
        key: bytes | None = ...,
        on_delivery: Callable[[Any, Any], None] | None = ...,
    ) -> None: ...
    def poll(self, timeout: float = ...) -> int: ...
    def flush(self, timeout: float = ...) -> int: ...


class DeliveryFailed(RuntimeError):
    pass


def days_to_poll(now: datetime, zone_tz: ZoneInfo = PARIS) -> list[date]:
    local = now.astimezone(zone_tz)
    today = local.date()
    if local.hour < YESTERDAY_UNTIL_HOUR:
        return [today - timedelta(days=1), today]
    return [today]


def poll_once(
    source: Source,
    producer: Producer,
    *,
    schema_id: int,
    now: datetime,
    zone: BiddingZone = BiddingZone.FR,
    zone_tz: ZoneInfo = PARIS,
    flush_seconds: float = 30.0,
) -> dict[str, Any]:
    """Fetch the day or days in play, publish every record and every refusal."""
    failures: list[str] = []

    def delivered(error: Any, message: Any) -> None:
        if error is not None:
            failures.append(str(error))

    summary: dict[str, Any] = {"published": 0, "quarantined": 0, "empty_days": []}
    for day in days_to_poll(now, zone_tz):
        start, end = settlement_day(day, zone_tz)
        try:
            fetched = source.actual_generation_per_type(zone.value, start, end)
        except EntsoeNoData:
            summary["empty_days"].append(day.isoformat())
            continue

        # when this poll learned it. to the second, so a value seen on two
        # polls carries two distinct known_at values and a consumer can tell
        # a repeat from a revision by comparing the quantity, not the stamp.
        known_at = fetched.fetched_at.astimezone(UTC).replace(microsecond=0)
        result = apply_gate(parse_generation(fetched.body), known_at)

        for record in result.records:
            producer.produce(
                topics.OBSERVATIONS.name,
                key=serialization.key(record),
                value=serialization.encode(record, schema_id),
                on_delivery=delivered,
            )
            # serves delivery callbacks as they come back, so the local
            # queue never fills during a large poll
            producer.poll(0)
        for violation in result.violations:
            producer.produce(
                topics.QUARANTINE.name,
                value=json.dumps(
                    {
                        "seen_at": known_at.isoformat(),
                        "reason": violation.reason,
                        "detail": violation.detail,
                        "payload": violation.payload,
                    },
                    sort_keys=True,
                ).encode("utf-8"),
                on_delivery=delivered,
            )
        summary["published"] += len(result.records)
        summary["quarantined"] += len(result.violations)
        summary.setdefault("days", []).append(
            {
                "day": day.isoformat(),
                "records": len(result.records),
                "gaps": len(result.gaps),
            }
        )

    unsent = producer.flush(flush_seconds)
    if unsent or failures:
        raise DeliveryFailed(
            f"{unsent} messages still queued, {len(failures)} refused: {failures[:3]}"
        )
    summary["polled_at"] = now.isoformat()
    return summary


def _token() -> str:
    import boto3

    parameter = os.environ.get("GRIDLENS_ENTSOE_TOKEN_PARAM", "/gridlens/entsoe/token")
    value: str = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)[
        "Parameter"
    ]["Value"]
    return value


def run(
    once: bool, interval: float, sleep: Callable[[float], None] = time.sleep
) -> None:
    import httpx
    from confluent_kafka import Producer as KafkaProducer
    from confluent_kafka.admin import AdminClient
    from confluent_kafka.cimpl import NewTopic

    from gridlens.ingest import redaction
    from gridlens.ingest.entsoe import EntsoeClient

    bootstrap = os.environ.get("GRIDLENS_KAFKA_BOOTSTRAP", "localhost:19092")
    registry = os.environ.get("GRIDLENS_SCHEMA_REGISTRY", "http://localhost:18081")

    token = _token()
    redaction.install(token)

    created = topics.ensure(AdminClient({"bootstrap.servers": bootstrap}), NewTopic)
    with httpx.Client(timeout=10) as http:
        schema_id = serialization.register(
            http, registry, f"{topics.OBSERVATIONS.name}-value"
        )
    log.info("topics created %s, schema id %s", created or "none", schema_id)

    producer = KafkaProducer(
        {
            "bootstrap.servers": bootstrap,
            # a retried send cannot land twice. duplicates in this topic should
            # only ever be the source repeating itself, never the transport.
            "enable.idempotence": True,
            "acks": "all",
            "linger.ms": 100,
            "compression.type": "lz4",
        }
    )

    stopping = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s, stopping after this poll", signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with EntsoeClient(token) as client:
        while not stopping:
            try:
                summary = poll_once(
                    client, producer, schema_id=schema_id, now=datetime.now(UTC)
                )
                log.info("poll done %s", summary)
            except Exception:
                # one bad poll is logged and the next one tries again. the
                # topic having a hole is better than the producer being dead.
                log.exception("poll failed")
                if once:
                    raise
            if once:
                break
            waited = 0.0
            while waited < interval and not stopping:
                sleep(1.0)
                waited += 1.0

    producer.flush(30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    run(args.once, args.interval)


if __name__ == "__main__":
    main()
