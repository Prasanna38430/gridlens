from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gridlens.quality.restatement import (
    AuditFailed,
    ProductionRun,
    check_clocks,
    compare,
    differences,
    last_month,
    production_run,
    restate,
    schema_for,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dbt_teardown import check_schema  # noqa: E402


class FakeAthena:
    def __init__(self, rows: list[list[str]], state: str = "SUCCEEDED") -> None:
        self.rows = rows
        self.state = state
        self.queries: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.queries.append(kwargs["QueryString"])
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "QueryExecution": {
                "Status": {"State": self.state, "StateChangeReason": "it did not work"}
            }
        }

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        header = {"Data": [{"VarCharValue": "_col"}]}
        body = [{"Data": [{"VarCharValue": v} for v in row]} for row in self.rows]
        return {"ResultSet": {"Rows": [header, *body]}}


def no_sleep(_: float) -> None:
    pass


RUN = ProductionRun(
    git_sha="cde02404363b543a074d6af4577441334b931e71",
    snapshot_id=8033667728052935381,
    learned_up_to=datetime(2026, 9, 30, 4, 30, 32, 473000, tzinfo=UTC),
)

AUGUST = last_month(date(2026, 9, 30))


def test_last_month_is_cut_at_paris_midnight_not_utc():
    september = last_month(date(2026, 10, 1))
    assert (september.first_day, september.last_day) == (
        date(2026, 9, 1),
        date(2026, 9, 30),
    )
    assert september.start == datetime(2026, 8, 31, 22, tzinfo=UTC)
    assert september.end == datetime(2026, 9, 30, 22, tzinfo=UTC)


def test_october_is_an_hour_longer_and_ends_on_winter_time():
    october = last_month(date(2026, 11, 15))
    assert october.start == datetime(2026, 9, 30, 22, tzinfo=UTC)
    assert october.end == datetime(2026, 10, 31, 23, tzinfo=UTC)
    assert october.end - october.start == timedelta(days=31, hours=1)


def test_march_is_an_hour_shorter_and_starts_on_winter_time():
    march = last_month(date(2027, 4, 1))
    assert march.start == datetime(2027, 2, 28, 23, tzinfo=UTC)
    assert march.end == datetime(2027, 3, 31, 22, tzinfo=UTC)
    assert march.end - march.start == timedelta(days=31, hours=-1)


def test_january_audits_december_of_the_year_before():
    december = last_month(date(2027, 1, 1))
    assert (december.first_day, december.last_day) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )


def test_the_audit_schema_is_one_the_teardown_will_drop():
    # if these drift apart every audit leaves its schema behind
    check_schema(schema_for(date(2026, 10, 1)))


def test_the_production_run_comes_from_the_newest_manifest_row():
    client = FakeAthena(
        [[RUN.git_sha, "8033667728052935381", "2026-09-30 04:30:32.473"]]
    )
    assert production_run(client, sleep=no_sleep) == RUN
    assert "ORDER BY run_started_at DESC" in client.queries[0]


def test_an_empty_manifest_is_refused():
    with pytest.raises(AuditFailed, match="empty"):
        production_run(FakeAthena([]), sleep=no_sleep)


def test_a_snapshot_id_that_is_not_a_number_never_reaches_sql():
    client = FakeAthena([["sha", "1; DROP TABLE x", "2026-09-30 04:30:32"]])
    with pytest.raises(AuditFailed, match="not a number"):
        production_run(client, sleep=no_sleep)


def test_clocks_that_agree_pass_and_the_query_checks_both_directions():
    client = FakeAthena([["43319", "43319", "0", "0"]])
    counts = check_clocks(client, AUGUST, RUN, sleep=no_sleep)
    assert counts["left"] == 43319
    sql = client.queries[0]
    assert "FOR VERSION AS OF 8033667728052935381" in sql
    assert "known_at <= TIMESTAMP '2026-09-30 04:30:32.473000'" in sql
    assert "valid_time >= TIMESTAMP '2026-07-31 22:00:00.000000'" in sql
    assert sql.count("EXCEPT") == 2


def test_a_row_committed_late_with_an_early_known_at_fails_the_check():
    # a backfill that landed after production ran, stamped before it
    client = FakeAthena([["43319", "43320", "0", "1"]])
    with pytest.raises(AuditFailed, match="disagree"):
        check_clocks(client, AUGUST, RUN, sleep=no_sleep)


def test_an_expired_snapshot_says_what_to_do():
    with pytest.raises(AuditFailed, match="rebuild production"):
        check_clocks(FakeAthena([], state="FAILED"), AUGUST, RUN, sleep=no_sleep)


class FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = "dbt output"
        self.stderr = ""


def test_restate_runs_dbt_on_the_audit_target_with_the_bound():
    calls: list[dict[str, Any]] = []

    def runner(command: list[str], **kwargs: Any) -> FakeResult:
        calls.append({"command": command, **kwargs})
        return FakeResult(0)

    restate("dbt", "transform", "gridlens_audit_20260930", AUGUST, RUN, runner=runner)

    command = calls[0]["command"]
    assert command[command.index("--target") + 1] == "audit"
    assert command[command.index("--exclude") + 1] == "run_manifest"
    assert json.loads(command[command.index("--vars") + 1]) == {
        "restate_as_of": "2026-09-30 04:30:32.473000",
        "restate_from": "2026-07-31 22:00:00.000000",
        "restate_to": "2026-08-31 22:00:00.000000",
    }
    assert calls[0]["env"]["DBT_AUDIT_SCHEMA"] == "gridlens_audit_20260930"
    # left alone unless asked, so a run on the host behaves like make dbt
    assert "--target-path" not in command


def test_the_container_keeps_its_dbt_artifacts_out_of_the_project():
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> FakeResult:
        calls.append(command)
        return FakeResult(0)

    restate(
        "dbt",
        "transform",
        "gridlens_audit_20260930",
        AUGUST,
        RUN,
        artifacts_dir="/tmp/dbt-audit",
        runner=runner,
    )
    command = calls[0]
    assert command[command.index("--target-path") + 1] == "/tmp/dbt-audit/target"
    assert command[command.index("--log-path") + 1] == "/tmp/dbt-audit/logs"


def test_a_failed_dbt_run_fails_the_restatement():
    with pytest.raises(AuditFailed, match="dbt exited 1"):
        restate(
            "dbt",
            "transform",
            "gridlens_audit_20260930",
            AUGUST,
            RUN,
            runner=lambda *a, **k: FakeResult(1),
        )


def test_every_table_is_compared_against_its_production_schema():
    client = FakeAthena([["741", "741", "0", "0"]])
    results = compare(client, "gridlens_audit_20260930", AUGUST, sleep=no_sleep)

    assert set(results) == {
        "generation_versions",
        "period_generation_net",
        "daily_generation_mix",
    }
    assert differences(results) == []
    assert "gridlens_silver.generation_versions" in client.queries[0]
    assert "gridlens_gold.daily_generation_mix" in client.queries[2]
    assert (
        "settlement_day BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'"
        in client.queries[2]
    )


@pytest.mark.parametrize(
    "counts",
    [
        {
            "restated": 364,
            "production": 364,
            "only_restated": 299,
            "only_production": 299,
        },
        {"restated": 364, "production": 365, "only_restated": 0, "only_production": 0},
    ],
)
def test_any_difference_at_all_is_reported(counts: dict[str, int]):
    assert differences({"daily_generation_mix": counts}) == ["daily_generation_mix"]
