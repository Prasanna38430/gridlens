from __future__ import annotations

from enum import StrEnum

# Checked against the platform's own area list. An EIC code existing is not the
# same as it being a bidding zone: 10YIT-GRTN-----B and 10YLU-CEGEDEL-NQ are
# both real codes with no BZN role, because Italy is split into zones and
# Luxembourg sits inside DE-LU. Neither belongs here.
#
# This enum is the answer to a specific problem. Entsoe returns the same
# "no matching data found" for a mistyped zone as it does for a genuinely empty
# window, so a typo is undetectable after the call and has to be caught before.


class BiddingZone(StrEnum):
    FR = "10YFR-RTE------C"
    BE = "10YBE----------2"
    NL = "10YNL----------L"
    DE_LU = "10Y1001A1001A82H"
    ES = "10YES-REE------0"
    PT = "10YPT-REN------W"
    CH = "10YCH-SWISSGRIDZ"
    AT = "10YAT-APG------L"
    GB = "10YGB----------A"


# entso-e common code list. B21 to B24 are network assets rather than
# generation, so they should never turn up in an A75 document, but they are
# valid psrType values and the gate should say which one it saw.
class ProductionType(StrEnum):
    BIOMASS = "B01"
    LIGNITE = "B02"
    COAL_DERIVED_GAS = "B03"
    GAS = "B04"
    HARD_COAL = "B05"
    OIL = "B06"
    OIL_SHALE = "B07"
    PEAT = "B08"
    GEOTHERMAL = "B09"
    HYDRO_PUMPED_STORAGE = "B10"
    HYDRO_RUN_OF_RIVER = "B11"
    HYDRO_RESERVOIR = "B12"
    MARINE = "B13"
    NUCLEAR = "B14"
    OTHER_RENEWABLE = "B15"
    SOLAR = "B16"
    WASTE = "B17"
    WIND_OFFSHORE = "B18"
    WIND_ONSHORE = "B19"
    OTHER = "B20"
    AC_LINK = "B21"
    DC_LINK = "B22"
    SUBSTATION = "B23"
    TRANSFORMER = "B24"
    ENERGY_STORAGE = "B25"


# storage moves energy both ways, so these appear once as generation and once
# as consumption in the same document
BIDIRECTIONAL = frozenset(
    {ProductionType.HYDRO_PUMPED_STORAGE, ProductionType.ENERGY_STORAGE}
)
