from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gridlens.contracts.reference import BiddingZone, ProductionType

# A unit error is the failure this catches. France peaks around 40 GW on a
# single production type, so 150 GW is comfortably impossible and a kilowatt
# figure mislabelled as megawatts would be a thousand times over it. This is a
# smell detector, not a statement about the grid.
MAX_QUANTITY_MW = Decimal("150000")

MAX_SCALE = 3


class GenerationRecord(BaseModel):
    """One settlement period of generation, as we knew it at known_at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["entsoe", "rte"]
    # entsoe gives every document an mRID. RTE has no equivalent, so this is
    # None there rather than a value invented to fill the column.
    source_document_id: str | None = None
    zone: BiddingZone
    production_type: ProductionType
    direction: Literal["generation", "consumption"]
    # canonical, not what either source calls it. entsoe says MAW, RTE says
    # nothing at all and means megawatts.
    unit: Literal["MW"]
    resolution_minutes: Annotated[int, Field(gt=0, le=1440)]
    valid_time: datetime
    known_at: datetime
    # when the source says it last changed this value. RTE publishes it, entsoe
    # does not, so it is optional and is never a substitute for known_at.
    source_updated_at: datetime | None = None
    quantity_mw: Decimal

    @field_validator("valid_time", "known_at", "source_updated_at")
    @classmethod
    def _must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"expected utc, got offset {value.utcoffset()}")
        return value.astimezone(UTC)

    @field_validator("quantity_mw")
    @classmethod
    def _plausible_quantity(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("negative generation")
        if value > MAX_QUANTITY_MW:
            raise ValueError(f"{value} MW is beyond anything a zone produces")
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and -exponent > MAX_SCALE:
            raise ValueError(f"{-exponent} decimal places, more than {MAX_SCALE}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> GenerationRecord:
        offset = self.valid_time - self.valid_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if offset % timedelta(minutes=self.resolution_minutes):
            raise ValueError(
                f"{self.valid_time.isoformat()} is not on a "
                f"{self.resolution_minutes} minute boundary"
            )

        # A75 with processType A16 is realised generation. Learning a realised
        # value before the period it describes has finished means either a
        # clock problem on our side or a forecast wearing the wrong label.
        if self.known_at < self.valid_time:
            raise ValueError("realised value known before the period it describes")

        return self
