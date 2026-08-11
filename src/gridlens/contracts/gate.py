from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from gridlens.contracts.records import GenerationRecord
from gridlens.ingest.entsoe_parse import Document, GenerationSeries
from gridlens.timeaxis import position_time


@dataclass(frozen=True)
class Violation:
    reason: str
    detail: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Gap:
    """A settlement period the source did not publish. Absence, not a failure."""

    zone: str
    production_type: str
    direction: str
    valid_time: datetime
    position: int


@dataclass(frozen=True)
class GateResult:
    records: tuple[GenerationRecord, ...] = ()
    violations: tuple[Violation, ...] = ()
    gaps: tuple[Gap, ...] = field(default=())

    @property
    def accepted(self) -> int:
        return len(self.records)


# entsoe speaks UN/CEFACT codes, we speak megawatts
UNITS = {"MAW": "MW"}


def build_record(candidate: dict[str, Any]) -> GenerationRecord | Violation:
    """Build a record, or the violation explaining why it could not be built."""
    try:
        return GenerationRecord(**candidate)
    except ValidationError as exc:
        return Violation(
            reason="contract",
            detail="; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            ),
            payload=_jsonable(candidate),
        )


def apply_gate(document: Document, known_at: datetime) -> GateResult:
    """Split a parsed document into records we accept and violations we keep.

    Never raises on bad data. A row this cannot build is a row to quarantine,
    because dropping it destroys the only evidence of what the source sent.
    """
    records: list[GenerationRecord] = []
    violations: list[Violation] = []
    gaps: list[Gap] = []

    for series in document.series:
        _gate_series(document, series, known_at, records, violations, gaps)

    return GateResult(tuple(records), tuple(violations), tuple(gaps))


def _gate_series(
    document: Document,
    series: GenerationSeries,
    known_at: datetime,
    records: list[GenerationRecord],
    violations: list[Violation],
    gaps: list[Gap],
) -> None:
    resolution_minutes, remainder = divmod(int(series.resolution.total_seconds()), 60)
    if remainder:
        violations.append(
            Violation(
                reason="sub_minute_resolution",
                detail=f"resolution {series.resolution} is not a whole minute",
                payload=_series_payload(document, series),
            )
        )
        return

    for observation in series.observations:
        candidate: dict[str, Any] = {
            "source": "entsoe",
            "source_document_id": document.mrid,
            "zone": series.zone,
            "production_type": series.production_type,
            "direction": series.direction,
            "unit": UNITS.get(series.unit, series.unit),
            "resolution_minutes": resolution_minutes,
            "valid_time": observation.valid_time,
            "known_at": known_at,
            "quantity_mw": observation.quantity,
        }
        built = build_record(candidate)
        if isinstance(built, GenerationRecord):
            records.append(built)
        else:
            violations.append(built)

    for position in series.missing_positions:
        gaps.append(
            Gap(
                zone=series.zone,
                production_type=series.production_type,
                direction=series.direction,
                valid_time=position_time(
                    series.interval_start, series.resolution, position
                ),
                position=position,
            )
        )


def _series_payload(document: Document, series: GenerationSeries) -> dict[str, Any]:
    return _jsonable(
        {
            "source": "entsoe",
            "source_document_id": document.mrid,
            "zone": series.zone,
            "production_type": series.production_type,
            "direction": series.direction,
            "unit": series.unit,
            "resolution": str(series.resolution),
            "interval_start": series.interval_start,
            "points": len(series.observations),
        }
    )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    # keep the original text of everything, so a quarantined row can be read
    # back and replayed without guessing at types
    return {
        k: (v.isoformat() if isinstance(v, datetime) else str(v))
        for k, v in payload.items()
    }
