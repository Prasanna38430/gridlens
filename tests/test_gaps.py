from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from gridlens.lake.gaps import GapQueryFailed, missing_settlement_days


class FakeAthena:
    def __init__(self, days: list[str], states: list[str] | None = None) -> None:
        self.days = days
        self.states = states or ["SUCCEEDED"]
        self.polls = 0
        self.queries: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.queries.append(kwargs["QueryString"])
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": "it did not work"}
            }
        }

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        header = {"Data": [{"VarCharValue": "missing_day"}]}
        body = [{"Data": [{"VarCharValue": d}]} for d in self.days]
        return {"ResultSet": {"Rows": [header, *body]}}


def find(days: list[str], **kwargs: Any) -> list[date]:
    client = FakeAthena(days)
    result = missing_settlement_days(
        client,
        start=kwargs.pop("start", date(2026, 8, 30)),
        end=kwargs.pop("end", date(2026, 9, 2)),
        sleep=lambda _: None,
        **kwargs,
    )
    find.last_query = client.queries[0]  # type: ignore[attr-defined]
    return result


def test_days_come_back_as_dates():
    assert find(["2026-08-31", "2026-09-01"]) == [date(2026, 8, 31), date(2026, 9, 1)]


def test_a_complete_window_returns_nothing():
    assert find([]) == []


def test_the_header_row_is_not_mistaken_for_a_day():
    # athena returns the header even when the result set is empty, and reading
    # it as data would report a gap called "missing_day" every single run
    client = FakeAthena([])
    assert (
        missing_settlement_days(
            client, start=date(2026, 9, 1), end=date(2026, 9, 1), sleep=lambda _: None
        )
        == []
    )


def test_the_query_carries_literal_timestamp_bounds():
    # a predicate only on the derived paris date gives athena no constant to
    # prune on and it reads the whole table
    find([], start=date(2026, 8, 30), end=date(2026, 9, 2))
    sql = find.last_query  # type: ignore[attr-defined]
    assert "TIMESTAMP '2026-08-29 00:00:00'" in sql
    assert "TIMESTAMP '2026-09-04 00:00:00'" in sql


def test_the_window_is_inclusive_at_both_ends():
    find([], start=date(2026, 9, 1), end=date(2026, 9, 2))
    sql = find.last_query  # type: ignore[attr-defined]
    assert "DATE '2026-09-01'" in sql
    assert "DATE '2026-09-02'" in sql


def test_a_backwards_window_is_refused():
    with pytest.raises(ValueError, match="end is before start"):
        missing_settlement_days(
            FakeAthena([]), start=date(2026, 9, 2), end=date(2026, 9, 1)
        )


def test_a_failed_query_raises_rather_than_reporting_no_gaps():
    # returning [] on failure would make a broken query look like a healthy
    # table, which is the worst possible failure mode for this function
    client = FakeAthena([], states=["FAILED"])
    with pytest.raises(GapQueryFailed, match="it did not work"):
        missing_settlement_days(
            client, start=date(2026, 9, 1), end=date(2026, 9, 1), sleep=lambda _: None
        )


def test_the_generated_day_series_is_cast_to_date():
    # sequence() over a day interval yields timestamps. without the cast the
    # query returns 2026-07-28 00:00:00 and date.fromisoformat refuses it.
    # the fake in this file cannot catch that, which is why it is asserted on
    # the sql rather than on the result.
    find([], start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert "CAST(d AS date)" in find.last_query  # type: ignore[attr-defined]
