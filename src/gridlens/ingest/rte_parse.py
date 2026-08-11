from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from gridlens.contracts.gate import Gap, GateResult, Violation, build_record
from gridlens.contracts.records import GenerationRecord
from gridlens.contracts.reference import BiddingZone, ProductionType

RESOURCE: Final = "actual_generations_per_production_type"

# RTE names the sectors, entsoe numbers them. Mapping to the entsoe codes makes
# the two comparable, which is the only reason a second source is worth having.
PRODUCTION_TYPES: Final[dict[str, ProductionType]] = {
    "BIOMASS": ProductionType.BIOMASS,
    "FOSSIL_GAS": ProductionType.GAS,
    "FOSSIL_HARD_COAL": ProductionType.HARD_COAL,
    "FOSSIL_OIL": ProductionType.OIL,
    "FOSSIL_BROWN_COAL": ProductionType.LIGNITE,
    "HYDRO_PUMPED_STORAGE": ProductionType.HYDRO_PUMPED_STORAGE,
    "HYDRO_RUN_OF_RIVER_AND_POUNDAGE": ProductionType.HYDRO_RUN_OF_RIVER,
    "HYDRO_WATER_RESERVOIR": ProductionType.HYDRO_RESERVOIR,
    "NUCLEAR": ProductionType.NUCLEAR,
    "SOLAR": ProductionType.SOLAR,
    "WASTE": ProductionType.WASTE,
    "WIND_OFFSHORE": ProductionType.WIND_OFFSHORE,
    "WIND_ONSHORE": ProductionType.WIND_ONSHORE,
    "EXCHANGE": ProductionType.OTHER,
}

# an aggregate sitting in the same list as its own components. it is excluded
# from records and used as an arithmetic check instead.
TOTAL: Final = "TOTAL"


def parse_actual_generation(
    body: bytes, known_at: datetime, zone: BiddingZone = BiddingZone.FR
) -> GateResult:
    """Normalise an RTE actual generation payload into contract records."""
    payload = json.loads(body)
    if RESOURCE not in payload:
        raise ValueError(f"expected a {RESOURCE} payload, got {sorted(payload)}")

    records: list[GenerationRecord] = []
    violations: list[Violation] = []
    gaps: list[Gap] = []

    for entry in payload[RESOURCE]:
        if entry.get("production_type") == TOTAL:
            continue
        _normalise_entry(entry, known_at, zone, records, violations, gaps)

    return GateResult(tuple(records), tuple(violations), tuple(gaps))


def total_matches_components(body: bytes) -> dict[str, tuple[int, int]]:
    """Hours where the TOTAL entry disagrees with the sum of its components.

    RTE ships the aggregate alongside the parts, so checking one against the
    other is free and catches a truncated response that still parses.
    """
    payload = json.loads(body)[RESOURCE]
    total = next((e for e in payload if e.get("production_type") == TOTAL), None)
    if total is None:
        return {}

    summed: dict[str, int] = {}
    for entry in payload:
        if entry.get("production_type") == TOTAL:
            continue
        for value in entry["values"]:
            summed[value["start_date"]] = summed.get(value["start_date"], 0) + int(
                value["value"]
            )

    return {
        v["start_date"]: (int(v["value"]), summed.get(v["start_date"], 0))
        for v in total["values"]
        if summed.get(v["start_date"], 0) != int(v["value"])
    }


def _normalise_entry(
    entry: dict[str, Any],
    known_at: datetime,
    zone: BiddingZone,
    records: list[GenerationRecord],
    violations: list[Violation],
    gaps: list[Gap],
) -> None:
    raw_type = str(entry.get("production_type", ""))
    production_type = PRODUCTION_TYPES.get(raw_type)
    if production_type is None:
        violations.append(
            Violation(
                reason="unknown_production_type",
                detail=f"{raw_type!r} has no entsoe equivalent",
                payload={"production_type": raw_type, "source": "rte"},
            )
        )
        return

    values = entry.get("values") or []
    if not values:
        return

    spans = {
        _instant(v["end_date"]) - _instant(v["start_date"])
        for v in values
        if "end_date" in v
    }
    if len(spans) != 1:
        violations.append(
            Violation(
                reason="mixed_resolution",
                detail=f"{raw_type} carries spans {sorted(str(s) for s in spans)}",
                payload={"production_type": raw_type, "source": "rte"},
            )
        )
        return
    resolution = spans.pop()

    present: dict[datetime, dict[str, Any]] = {
        _instant(v["start_date"]): v for v in values
    }

    for moment in _expected(
        _instant(entry["start_date"]), _instant(entry["end_date"]), resolution
    ):
        value = present.get(moment)
        if value is None:
            # RTE gives no positions and no nulls, so an absent period is only
            # visible by building the grid the window implies and diffing. it
            # drops the repeated hour every october and says nothing.
            gaps.append(
                Gap(
                    zone=zone.value,
                    production_type=production_type.value,
                    direction="generation",
                    valid_time=moment,
                    position=0,
                )
            )
            continue

        quantity = Decimal(str(value["value"]))
        # pumping shows up as negative generation here, where entsoe emits a
        # separate consumption series. split the sign into a direction so the
        # two sources describe the same thing the same way.
        direction = "consumption" if quantity < 0 else "generation"

        built = build_record(
            {
                "source": "rte",
                "source_document_id": None,
                "zone": zone,
                "production_type": production_type,
                "direction": direction,
                "unit": "MW",
                "resolution_minutes": int(resolution.total_seconds() // 60),
                "valid_time": moment,
                "known_at": known_at,
                "source_updated_at": _optional_instant(value.get("updated_date")),
                "quantity_mw": abs(quantity),
            }
        )
        if isinstance(built, GenerationRecord):
            records.append(built)
        else:
            violations.append(built)


def _expected(start: datetime, end: datetime, resolution: timedelta) -> list[datetime]:
    moments = []
    moment = start
    while moment < end:
        moments.append(moment)
        moment += resolution
    return moments


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp without an offset: {text!r}")
    return moment.astimezone(UTC)


def _optional_instant(text: str | None) -> datetime | None:
    return _instant(text) if text else None
