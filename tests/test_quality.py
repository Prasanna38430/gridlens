from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from gridlens.quality import queries
from gridlens.quality.bronze import completeness, fetch, invariants, run

HEALTHY_INVARIANTS = {
    "duplicate_keys": ["0"],
    "future_periods": ["0"],
    "known_before_it_happened": ["0"],
    "negative_quantities": ["0"],
    "hours_since_last_learned": ["6"],
}

HEALTHY_COMPLETENESS = {
    "zone": ["10YFR-RTE------C"] * 3,
    "settlement_day": ["2026-08-20", "2026-08-21", "2026-08-22"],
    "production_type": ["B14", "B16", "B10"],
    "direction": ["generation", "generation", "consumption"],
    "periods": ["96", "70", "96"],
    "versions": ["1", "1", "2"],
    "max_mw": ["39733.29", "8100.5", "2788.0"],
}


def frame(base: dict[str, list[str]], **overrides: list[str]) -> pd.DataFrame:
    return pd.DataFrame({**base, **overrides})


class FakeAthena:
    """Returns canned result sets in the shape Athena actually sends."""

    def __init__(self, frames: list[pd.DataFrame]) -> None:
        self.frames = frames
        self.queries: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.queries.append(kwargs["QueryString"])
        return {"QueryExecutionId": f"q-{len(self.queries)}"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": 1000},
            }
        }

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        df = self.frames[int(kwargs["QueryExecutionId"].split("-")[1]) - 1]
        rows = [{"Data": [{"VarCharValue": c} for c in df.columns]}]
        rows += [
            {"Data": [{"VarCharValue": str(v)} for v in row]}
            for row in df.itertuples(index=False)
        ]
        return {"ResultSet": {"Rows": rows}}


def failed_columns(result: Any) -> set[str]:
    return {
        r.expectation_config.kwargs.get("column")
        for r in result.results
        if not r.success
    }


def test_a_healthy_table_passes_everything():
    assert invariants(frame(HEALTHY_INVARIANTS)).success
    assert completeness(frame(HEALTHY_COMPLETENESS)).success


def test_a_duplicate_bitemporal_key_is_caught():
    # the merge is supposed to make this impossible, so it failing means the
    # writer is broken rather than the data
    result = invariants(frame(HEALTHY_INVARIANTS, duplicate_keys=["3"]))
    assert not result.success
    assert failed_columns(result) == {"duplicate_keys"}


def test_a_stale_table_is_caught():
    result = invariants(frame(HEALTHY_INVARIANTS, hours_since_last_learned=["73"]))
    assert not result.success
    assert failed_columns(result) == {"hours_since_last_learned"}


def test_a_value_known_before_its_period_is_caught():
    result = invariants(frame(HEALTHY_INVARIANTS, known_before_it_happened=["1"]))
    assert not result.success


def test_a_short_settlement_day_is_caught():
    # this is the real 2026-08-24 case: entso-e had not published a full day
    result = completeness(frame(HEALTHY_COMPLETENESS, periods=["96", "37", "96"]))
    assert not result.success
    assert failed_columns(result) == {"periods"}


def test_a_day_with_too_many_periods_is_caught():
    # 100 is legal in october, 104 is never legal
    result = completeness(frame(HEALTHY_COMPLETENESS, periods=["96", "104", "96"]))
    assert not result.success


def test_a_kilowatt_figure_mislabelled_as_megawatts_is_caught():
    result = completeness(
        frame(HEALTHY_COMPLETENESS, max_mw=["39733.29", "8100.5", "39733290"])
    )
    assert not result.success
    assert failed_columns(result) == {"max_mw"}


def test_an_unknown_direction_is_caught():
    result = completeness(
        frame(HEALTHY_COMPLETENESS, direction=["generation", "sideways", "consumption"])
    )
    assert not result.success


def test_fetch_reads_the_athena_result_shape():
    client = FakeAthena([frame(HEALTHY_INVARIANTS)])
    df, scanned = fetch(client, "SELECT 1", sleep=lambda _: None)

    assert list(df.columns) == list(HEALTHY_INVARIANTS)
    assert df.loc[0, "hours_since_last_learned"] == "6"
    assert scanned == 1000


def test_run_reports_every_failure_and_the_bytes_it_cost():
    client = FakeAthena(
        [
            frame(HEALTHY_INVARIANTS, duplicate_keys=["2"]),
            frame(HEALTHY_COMPLETENESS, periods=["96", "12", "96"]),
        ]
    )
    result = run(client)

    assert not result["passed"]
    assert result["expectations"] == 10
    assert len(result["failed"]) == 2
    assert result["scanned_bytes"] == 2000
    assert any("duplicate_keys" in f for f in result["failed"])
    assert any("periods" in f for f in result["failed"])


def test_completeness_is_grained_on_the_settlement_day_not_the_utc_date():
    # a paris day runs 22:00Z to 22:00Z. grouping on the utc date splits every
    # fetch in two and reports 8 periods on one date and 88 on the next.
    assert "AT TIME ZONE 'Europe/Paris'" in queries.COMPLETENESS
    assert "settlement_day < newest.latest" in queries.COMPLETENESS


def test_a_failing_query_raises_rather_than_returning_empty():
    class Failing(FakeAthena):
        def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "QueryExecution": {
                    "Status": {"State": "FAILED", "StateChangeReason": "table gone"}
                }
            }

    with pytest.raises(RuntimeError, match="table gone"):
        fetch(Failing([]), "SELECT 1", sleep=lambda _: None)
