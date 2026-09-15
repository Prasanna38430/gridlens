from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from athena import strip_leading_comments  # noqa: E402

MAINTENANCE = Path(__file__).resolve().parents[1] / "sql" / "maintenance"


def test_a_header_comment_is_removed():
    # athena routes its iceberg extensions on the first keyword, so a comment
    # in front of OPTIMIZE gets the statement parsed as ordinary sql and
    # rejected with "mismatched input"
    sql = "-- why this exists\n-- a second line\n\nOPTIMIZE t REWRITE DATA"
    assert strip_leading_comments(sql).startswith("OPTIMIZE")


def test_comments_after_the_first_keyword_are_left_alone():
    sql = "SELECT 1\n-- trailing note\nFROM t"
    assert strip_leading_comments(sql) == "SELECT 1\n-- trailing note\nFROM t"


def test_a_statement_with_no_comment_is_unchanged():
    assert strip_leading_comments("SELECT 1") == "SELECT 1"


def test_a_file_of_only_comments_yields_nothing():
    assert strip_leading_comments("-- nothing here\n\n-- still nothing") == ""


def test_a_double_dash_inside_the_statement_is_not_a_leading_comment():
    sql = "SELECT '--not a comment' AS x"
    assert strip_leading_comments(sql) == sql


def test_every_maintenance_file_starts_with_a_keyword_after_stripping():
    # the regression this guards: both files shipped with header comments and
    # neither had ever been run through scripts/athena.py
    files = sorted(MAINTENANCE.glob("*.sql"))
    assert files, "no maintenance sql found"
    for path in files:
        first = strip_leading_comments(path.read_text(encoding="utf-8")).split()[0]
        assert first.upper() in {"OPTIMIZE", "VACUUM", "ALTER"}, f"{path.name}: {first}"
