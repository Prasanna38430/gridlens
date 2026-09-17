from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dbt_ci_teardown import (  # noqa: E402
    DropFailed,
    RefusedSchema,
    check_schema,
    teardown,
)


class Pager:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.pages


class NotFound(Exception):
    response = {"Error": {"Code": "EntityNotFoundException"}}


class FakeGlue:
    def __init__(self, tables: list[dict[str, str]] | None) -> None:
        self.tables = tables
        self.deleted: list[str] = []

    def get_paginator(self, name: str, /) -> Any:
        tables = self.tables

        class P:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                if tables is None:
                    raise NotFound()
                return [{"TableList": tables}]

        return P()

    def delete_database(self, **kwargs: Any) -> dict[str, str]:
        self.deleted.append(kwargs["Name"])
        return {}


class FakeAthena:
    def __init__(self, state: str = "SUCCEEDED") -> None:
        self.state = state
        self.sql: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, str]:
        self.sql.append(kwargs["QueryString"])
        return {"QueryExecutionId": "q"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "QueryExecution": {
                "Status": {"State": self.state, "StateChangeReason": "denied"}
            }
        }


class FakeS3:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.prefix: str | None = None
        self.deleted: list[str] = []

    def get_paginator(self, name: str, /) -> Any:
        outer = self

        class P:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                outer.prefix = kwargs["Prefix"]
                return [{"Contents": [{"Key": k} for k in outer.keys]}]

        return P()

    def delete_objects(self, **kwargs: Any) -> dict[str, str]:
        self.deleted += [o["Key"] for o in kwargs["Delete"]["Objects"]]
        return {}


SCHEMA = "gridlens_ci_pr_14"


@pytest.mark.parametrize(
    "name", ["gridlens_silver", "gridlens_gold", "gridlens_bronze", "gridlens_ci_"]
)
def test_production_schemas_are_refused(name: str):
    # checked in the script as well as in iam, so a typo fails here first
    with pytest.raises(RefusedSchema):
        check_schema(name)


def test_a_name_that_could_carry_sql_is_refused():
    with pytest.raises(RefusedSchema):
        check_schema("gridlens_ci_x`; DROP TABLE gridlens_gold.run_manifest; --")


def test_nothing_is_touched_before_the_name_is_checked():
    glue, athena = FakeGlue([{"Name": "t"}]), FakeAthena()
    with pytest.raises(RefusedSchema):
        teardown(glue, athena, "gridlens_gold", sleep=lambda _: None)
    assert athena.sql == []
    assert glue.deleted == []


def test_tables_and_views_are_dropped_through_athena_then_the_schema():
    glue = FakeGlue(
        [
            {"Name": "generation_versions", "TableType": "EXTERNAL_TABLE"},
            {"Name": "generation_current", "TableType": "VIRTUAL_VIEW"},
        ]
    )
    athena = FakeAthena()
    result = teardown(glue, athena, SCHEMA, sleep=lambda _: None)

    # the quoting differs by relation and this is the regression: athena
    # rejects a backticked DROP VIEW outright, and the first version of this
    # test asserted backticks for both, so it could not have caught it
    assert athena.sql == [
        f"DROP TABLE IF EXISTS `{SCHEMA}`.`generation_versions`",
        f'DROP VIEW IF EXISTS "{SCHEMA}"."generation_current"',
    ]
    assert glue.deleted == [SCHEMA]
    assert result["dropped"] == ["generation_versions", "generation_current"]


def test_a_schema_that_was_never_created_is_not_an_error():
    # a pull request that changes no models builds nothing, and teardown still
    # runs because it is an always step
    glue, athena = FakeGlue(None), FakeAthena()
    result = teardown(glue, athena, SCHEMA, sleep=lambda _: None)
    assert result["existed"] is False
    assert athena.sql == [] and glue.deleted == []


def test_a_failed_drop_stops_before_deleting_the_schema():
    # deleting the catalog entry after a failed iceberg drop would orphan the
    # table's files with nothing left pointing at them
    glue = FakeGlue([{"Name": "t", "TableType": "EXTERNAL_TABLE"}])
    with pytest.raises(DropFailed, match="denied"):
        teardown(glue, FakeAthena("FAILED"), SCHEMA, sleep=lambda _: None)
    assert glue.deleted == []


def test_the_sweep_is_confined_to_the_schema_prefix():
    s3 = FakeS3([f"ci/{SCHEMA}/t/uuid/data.parquet"])
    teardown(
        FakeGlue([]),
        FakeAthena(),
        SCHEMA,
        store=s3,
        data_prefix="s3://gridlens-lake-054129814335/ci/",
        sleep=lambda _: None,
    )
    assert s3.prefix == f"ci/{SCHEMA}/"
    assert s3.deleted == [f"ci/{SCHEMA}/t/uuid/data.parquet"]


def test_a_view_is_never_dropped_with_backticks():
    # athena parses DROP VIEW on the trino engine and refuses the query before
    # it starts: "Queries of this type are not supported"
    athena = FakeAthena()
    teardown(
        FakeGlue([{"Name": "v", "TableType": "VIRTUAL_VIEW"}]),
        athena,
        SCHEMA,
        sleep=lambda _: None,
    )
    assert "`" not in athena.sql[0]
    assert athena.sql[0] == f'DROP VIEW IF EXISTS "{SCHEMA}"."v"'
