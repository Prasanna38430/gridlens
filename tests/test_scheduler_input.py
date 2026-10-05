from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

LAMBDA_TF = (
    Path(__file__).resolve().parents[1] / "infra" / "terraform" / "core" / "lambda.tf"
)

PLACEHOLDER = "<aws.scheduler.scheduled-time>"

INPUT_LINE = re.compile(r"^\s*input\s*=\s*(.+)$", re.MULTILINE)
SCHEDULE = re.compile(r'^resource "aws_scheduler_schedule" "(\w+)"', re.MULTILINE)


def scheduler_inputs() -> dict[str, str]:
    """Each schedule's name and the raw right hand side of its input."""
    body = LAMBDA_TF.read_text(encoding="utf-8")
    found = {}
    for match in SCHEDULE.finditer(body):
        line = INPUT_LINE.search(body, match.end())
        assert line, f"{match.group(1)} has no input assignment"
        found[match.group(1)] = line.group(1).strip()
    return found


def payload(raw: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(raw.strip('"').replace('\\"', '"'))
    return loaded


def test_both_schedules_are_checked():
    # a third schedule added without these checks would go untested silently
    assert set(scheduler_inputs()) == {"ingest_entsoe", "refetch_entsoe"}


@pytest.mark.parametrize("name", ["ingest_entsoe", "refetch_entsoe"])
def test_the_placeholder_is_written_literally(name: str):
    # jsonencode escapes the angle brackets and the scheduler then substitutes
    # nothing, which is what broke every run from 2026-08-26.
    assert PLACEHOLDER in scheduler_inputs()[name]


@pytest.mark.parametrize("name", ["ingest_entsoe", "refetch_entsoe"])
def test_the_input_is_not_built_with_jsonencode(name: str):
    assert "jsonencode" not in scheduler_inputs()[name]


def test_the_ingest_input_carries_known_at_only():
    assert payload(scheduler_inputs()["ingest_entsoe"]) == {"known_at": PLACEHOLDER}


def test_the_refetch_input_carries_known_at_and_a_window():
    assert payload(scheduler_inputs()["refetch_entsoe"]) == {
        "known_at": PLACEHOLDER,
        "refetch_days": 27,
    }


@pytest.mark.parametrize("escaped", ["\\u003c", "\\u003e"])
def test_no_escaped_angle_bracket_survives_anywhere_in_the_file(escaped: str):
    assert escaped not in LAMBDA_TF.read_text(encoding="utf-8")
