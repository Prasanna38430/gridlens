from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from gridlens.quality import queries
from gridlens.quality.bronze import (
    completeness,
    expected_periods,
    fetch,
    invariants,
    run,
)

HEALTHY_INVARIANTS = {
    "duplicate_keys": ["0"],
    "future_periods": ["0"],
    "known_before_it_happened": ["0"],
    "negative_quantities": ["0"],
    "hours_since_last_learned": ["6"],
}

# One ordinary day and both clock changes, so the healthy frame already proves
# the day length is not hardcoded at 96.
HEALTHY_COMPLETENESS = {
    "zone": ["10YFR-RTE------C"] * 3,
    "settlement_day": ["2026-08-20", "2026-03-29", "2026-10-25"],
    "periods": ["96", "92", "100"],
    "series": ["15", "15", "16"],
    "densest_series": ["96", "92", "100"],
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
    failed = set()
    for r in result.results:
        if not r.success:
            kwargs = r.expectation_config.kwargs
            failed.add(kwargs.get("column") or kwargs.get("column_A"))
    return failed


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


@pytest.mark.parametrize(
    ("day", "periods"),
    [("2026-03-29", 92), ("2026-08-20", 96), ("2026-10-25", 100)],
)
def test_day_length_follows_the_clock_changes(day: str, periods: int):
    assert expected_periods(day) == periods


def test_a_short_settlement_day_is_caught():
    # the real 2026-08-29: fetched before entso-e had finished publishing, so
    # the day as a whole held 84 periods
    result = completeness(
        frame(
            HEALTHY_COMPLETENESS,
            periods=["84", "92", "100"],
            densest_series=["84", "92", "100"],
        )
    )
    assert not result.success
    assert failed_columns(result) == {"periods"}


def test_sparse_solar_does_not_fail_a_complete_day():
    # the regression this change exists for. by september solar publishes 62
    # periods because it has nothing to report at night, and the old per series
    # floor of 70 failed every morning on a day that was entirely complete. the
    # day grain cannot see one sparse series as long as the day is covered.
    result = completeness(frame(HEALTHY_COMPLETENESS))
    assert result.success


def test_a_clock_change_day_is_not_judged_against_96():
    # 96 periods on the march change is four too many, not a complete day
    result = completeness(
        frame(
            HEALTHY_COMPLETENESS,
            periods=["96", "96", "100"],
            densest_series=["96", "96", "100"],
        )
    )
    assert not result.success
    assert "periods" in failed_columns(result)


def test_a_series_with_more_periods_than_the_day_is_caught():
    result = completeness(
        frame(HEALTHY_COMPLETENESS, densest_series=["104", "92", "100"])
    )
    assert not result.success
    assert failed_columns(result) == {"expected_periods"}


def test_a_missing_series_is_caught():
    result = completeness(frame(HEALTHY_COMPLETENESS, series=["14", "15", "16"]))
    assert not result.success
    assert failed_columns(result) == {"series"}


def test_a_sixteenth_series_is_allowed():
    # real: coal published a zero as generation on 2026-09-02 and offshore wind
    # published consumption on 2026-09-14. a known type in an unusual direction
    # is not a defect.
    assert completeness(frame(HEALTHY_COMPLETENESS, series=["16", "16", "16"])).success


def test_a_kilowatt_figure_mislabelled_as_megawatts_is_caught():
    result = completeness(
        frame(HEALTHY_COMPLETENESS, max_mw=["39733.29", "8100.5", "39733290"])
    )
    assert not result.success
    assert failed_columns(result) == {"max_mw"}


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
            frame(
                HEALTHY_COMPLETENESS,
                periods=["96", "12", "100"],
                densest_series=["96", "12", "100"],
            ),
        ]
    )
    result = run(client)

    assert not result["passed"]
    assert result["expectations"] == 10
    assert len(result["failed"]) == 2
    assert result["scanned_bytes"] == 2000
    assert any("duplicate_keys" in f for f in result["failed"])
    # a pair expectation names column_A, and the report must not say None
    assert any("completeness.periods" in f for f in result["failed"])
    assert not any("None" in f for f in result["failed"])


def test_completeness_is_grained_on_the_settlement_day_not_the_utc_date():
    # a paris day runs 22:00Z to 22:00Z. grouping on the utc date splits every
    # fetch in two and reports 8 periods on one date and 88 on the next.
    assert "AT TIME ZONE 'Europe/Paris'" in queries.COMPLETENESS
    assert "settlement_day < newest.latest" in queries.COMPLETENESS


def test_completeness_is_grained_on_the_day_not_the_series():
    # the final select must not group by production type, or one sparse series
    # is back to failing a complete day
    final = queries.COMPLETENESS.rsplit("FROM per_day", 1)[0]
    assert "p.periods" in final
    assert "production_type" not in final.rsplit("SELECT", 1)[1]


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
