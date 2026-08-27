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


def scheduler_input() -> str:
    """The raw right hand side of the schedule target's input assignment."""
    body = LAMBDA_TF.read_text(encoding="utf-8")
    start = body.index('resource "aws_scheduler_schedule"')
    match = INPUT_LINE.search(body, start)
    assert match, "the schedule target has no input assignment"
    return match.group(1).strip()


def test_the_placeholder_is_written_literally():
    # jsonencode escapes the angle brackets and the scheduler then substitutes
    # nothing, which is what broke every run from 2026-08-26.
    assert PLACEHOLDER in scheduler_input()


def test_the_input_is_not_built_with_jsonencode():
    assert "jsonencode" not in scheduler_input()


def test_the_input_is_json_carrying_known_at():
    payload = json.loads(scheduler_input().strip('"').replace('\\"', '"'))
    assert payload == {"known_at": PLACEHOLDER}


@pytest.mark.parametrize("escaped", ["\\u003c", "\\u003e"])
def test_no_escaped_angle_bracket_survives_anywhere_in_the_file(escaped: str):
    assert escaped not in LAMBDA_TF.read_text(encoding="utf-8")
