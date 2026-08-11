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


class FileQuarantine:
    """Newline-delimited json under hive-style partitions.

    The layout matches what the S3 sink will use, so the local path and the
    bucket key are the same string with a different prefix. That is the point:
    a quarantined row read back from disk in a test is the same shape as one
    read back from the lake.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, violations: Iterable[Violation], seen_at: datetime) -> int:
        rows = list(violations)
        if not rows:
            # no empty file. an empty object still costs a request and makes
            # "did anything fail today" a question about file size.
            return 0

        target = self._root / self.partition(seen_at) / f"{uuid.uuid4().hex}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(_row(row, seen_at), sort_keys=True) + "\n")
        return len(rows)

    @staticmethod
    def partition(seen_at: datetime) -> str:
        return f"seen_date={seen_at:%Y-%m-%d}"


def _row(violation: Violation, seen_at: datetime) -> dict[str, Any]:
    return {
        "seen_at": seen_at.isoformat(),
        "reason": violation.reason,
        "detail": violation.detail,
        "payload": violation.payload,
    }
