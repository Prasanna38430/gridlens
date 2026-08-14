from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from gridlens.contracts.gate import Violation


class QuarantineSink(Protocol):
    def write(self, violations: Iterable[Violation], seen_at: datetime) -> int: ...


class ObjectStore(Protocol):
    """The one S3 call this needs.

    Narrower than boto3's client on purpose: typing the real thing means
    boto3-stubs, and a fake in a test means implementing one method.
    """

    def put_object(self, **kwargs: Any) -> Any: ...


def partition(seen_at: datetime) -> str:
    return f"seen_date={seen_at:%Y-%m-%d}"


def serialise(violations: Iterable[Violation], seen_at: datetime) -> bytes:
    return "".join(
        json.dumps(
            {
                "seen_at": seen_at.isoformat(),
                "reason": v.reason,
                "detail": v.detail,
                "payload": v.payload,
            },
            sort_keys=True,
        )
        + "\n"
        for v in violations
    ).encode("utf-8")


class FileQuarantine:
    """Newline-delimited json under hive-style partitions, on local disk.

    Same key layout as the S3 sink, so a row read back in a test has the same
    shape and path as one read back from the lake.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, violations: Iterable[Violation], seen_at: datetime) -> int:
        rows = list(violations)
        if not rows:
            # no empty file. an empty object still costs a request and turns
            # "did anything fail today" into a question about file size.
            return 0

        target = self._root / partition(seen_at) / f"{uuid.uuid4().hex}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(serialise(rows, seen_at))
        return len(rows)


class S3Quarantine:
    def __init__(self, bucket: str, client: ObjectStore, prefix: str = "") -> None:
        self._bucket = bucket
        self._client = client
        self._prefix = prefix.strip("/")

    def write(self, violations: Iterable[Violation], seen_at: datetime) -> int:
        rows = list(violations)
        if not rows:
            return 0

        self._client.put_object(
            Bucket=self._bucket,
            Key=self.key(seen_at),
            Body=serialise(rows, seen_at),
            ContentType="application/x-ndjson",
        )
        return len(rows)

    def key(self, seen_at: datetime) -> str:
        # a uuid rather than a counter, because two lambdas writing the same
        # partition must not be able to choose the same key. s3 has no
        # create-if-absent, so a collision is a silent overwrite.
        name = f"{partition(seen_at)}/{uuid.uuid4().hex}.jsonl"
        return f"{self._prefix}/{name}" if self._prefix else name
