from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from gridlens.contracts.records import GenerationRecord

# Athena reads these back with a plain CAST to timestamp, which has no opinion
# about zones. Writing an offset here would make that cast depend on the query
# session's timezone, so the offset is stripped and UTC is the invariant the
# contract layer already guarantees.
TIMESTAMP = "%Y-%m-%d %H:%M:%S.%f"


class ObjectStore(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...


def _instant(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).strftime(TIMESTAMP) if moment else None


def serialise(records: Iterable[GenerationRecord]) -> bytes:
    """Newline-delimited json, every field a string Athena will cast."""
    return "".join(
        json.dumps(
            {
                "source": r.source,
                "source_document_id": r.source_document_id,
                "zone": r.zone.value,
                "production_type": r.production_type.value,
                "direction": r.direction,
                "unit": r.unit,
                "resolution_minutes": str(r.resolution_minutes),
                "valid_time": _instant(r.valid_time),
                "known_at": _instant(r.known_at),
                "source_updated_at": _instant(r.source_updated_at),
                # str() on a Decimal keeps the exact digits. float would not.
                "quantity_mw": str(r.quantity_mw),
            },
            sort_keys=True,
        )
        + "\n"
        for r in records
    ).encode("utf-8")


class StagingArea:
    """Writes a batch of records where Athena can read them once."""

    def __init__(
        self, bucket: str, client: ObjectStore, prefix: str = "staging"
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._prefix = prefix.strip("/")

    def write(self, records: Iterable[GenerationRecord]) -> tuple[str, int]:
        """Stage a batch and return its id and row count."""
        rows = list(records)
        if not rows:
            return "", 0

        batch_id = uuid.uuid4().hex
        self._client.put_object(
            Bucket=self._bucket,
            Key=f"{self._prefix}/generation/batch_id={batch_id}/part.jsonl",
            Body=serialise(rows),
            ContentType="application/x-ndjson",
        )
        return batch_id, len(rows)
