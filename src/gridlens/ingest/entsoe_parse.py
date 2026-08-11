from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from gridlens.timeaxis import parse_resolution, period_count, position_time

Direction = Literal["generation", "consumption"]


@dataclass(frozen=True)
class Observation:
    valid_time: datetime
    position: int
    quantity: Decimal


@dataclass(frozen=True)
class GenerationSeries:
    zone: str
    production_type: str
    direction: Direction
    unit: str
    resolution: timedelta
    interval_start: datetime
    interval_end: datetime
    observations: tuple[Observation, ...]
    missing_positions: tuple[int, ...]

    @property
    def expected_positions(self) -> int:
        return len(self.observations) + len(self.missing_positions)


def _local(tag: str) -> str:
    # findtext with a dotted tag name is asking for trouble, so walk children
    # and strip the namespace by hand
    return tag.rpartition("}")[2]


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp without an offset: {text!r}")
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"expected utc, got {text!r}")
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class Document:
    mrid: str
    revision_number: int
    document_type: str
    process_type: str
    created_at: datetime
    series: tuple[GenerationSeries, ...]


def parse_generation(body: bytes) -> Document:
    """Turn an A75 document into series with real UTC timestamps on every point."""
    root = ET.fromstring(body)
    if _local(root.tag) != "GL_MarketDocument":
        raise ValueError(f"expected GL_MarketDocument, got {_local(root.tag)!r}")

    header: dict[str, str] = {}
    series: list[GenerationSeries] = []
    for child in root:
        name = _local(child.tag)
        if name == "TimeSeries":
            series.append(_parse_series(child))
        elif not len(child):
            header[name] = (child.text or "").strip()

    return Document(
        mrid=header.get("mRID", ""),
        revision_number=int(header.get("revisionNumber", "0")),
        document_type=header.get("type", ""),
        process_type=header.get("process.processType", ""),
        # note this is when the platform rendered the document, not when the
        # figures were published. requesting october 2025 today stamps it with
        # today, so it is provenance and never known_at.
        created_at=_instant(header["createdDateTime"]),
        series=tuple(series),
    )


def _parse_series(node: ET.Element) -> GenerationSeries:
    production_type = ""
    unit = ""
    in_zone = ""
    out_zone = ""
    period: ET.Element | None = None

    for child in node:
        name = _local(child.tag)
        if name == "MktPSRType":
            for sub in child:
                if _local(sub.tag) == "psrType":
                    production_type = (sub.text or "").strip()
        elif name == "inBiddingZone_Domain.mRID":
            in_zone = (child.text or "").strip()
        elif name == "outBiddingZone_Domain.mRID":
            out_zone = (child.text or "").strip()
        elif name == "quantity_Measure_Unit.name":
            unit = (child.text or "").strip()
        elif name == "Period":
            if period is not None:
                raise ValueError("more than one Period in a TimeSeries")
            period = child

    if period is None:
        raise ValueError("TimeSeries with no Period")

    # a pumped storage or battery series appears twice, once each way. summing
    # by production type without looking at this double counts storage.
    if in_zone and out_zone:
        raise ValueError("TimeSeries claims both directions")
    if in_zone:
        direction: Direction = "generation"
        zone = in_zone
    elif out_zone:
        direction = "consumption"
        zone = out_zone
    else:
        raise ValueError("TimeSeries names no bidding zone")

    return _parse_period(period, zone, production_type, direction, unit)


def _parse_period(
    node: ET.Element,
    zone: str,
    production_type: str,
    direction: Direction,
    unit: str,
) -> GenerationSeries:
    resolution: timedelta | None = None
    start: datetime | None = None
    end: datetime | None = None
    quantities: dict[int, Decimal] = {}

    for child in node:
        name = _local(child.tag)
        if name == "resolution":
            resolution = parse_resolution((child.text or "").strip())
        elif name == "timeInterval":
            for sub in child:
                if _local(sub.tag) == "start":
                    start = _instant((sub.text or "").strip())
                elif _local(sub.tag) == "end":
                    end = _instant((sub.text or "").strip())
        elif name == "Point":
            position, quantity = _parse_point(child)
            if position in quantities:
                raise ValueError(f"position {position} appears twice")
            quantities[position] = quantity

    if resolution is None or start is None or end is None:
        raise ValueError("Period missing resolution or timeInterval")

    expected = period_count(start, end, resolution)
    beyond = [p for p in quantities if p > expected]
    if beyond:
        raise ValueError(f"positions past the end of the interval: {sorted(beyond)}")

    observations = tuple(
        Observation(
            valid_time=position_time(start, resolution, position),
            position=position,
            quantity=quantities[position],
        )
        for position in sorted(quantities)
    )
    missing = tuple(p for p in range(1, expected + 1) if p not in quantities)

    return GenerationSeries(
        zone=zone,
        production_type=production_type,
        direction=direction,
        unit=unit,
        resolution=resolution,
        interval_start=start,
        interval_end=end,
        observations=observations,
        missing_positions=missing,
    )


def _parse_point(node: ET.Element) -> tuple[int, Decimal]:
    position: int | None = None
    quantity: Decimal | None = None

    for child in node:
        name = _local(child.tag)
        if name == "position":
            position = int((child.text or "").strip())
        elif name == "quantity":
            # decimal, not float. these are summed into settlement figures and
            # float addition is not associative.
            quantity = Decimal((child.text or "").strip())

    if position is None or quantity is None:
        raise ValueError("Point missing position or quantity")
    return position, quantity
