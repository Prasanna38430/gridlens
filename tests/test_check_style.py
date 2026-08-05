import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_style import check_commit_message, check_prose  # noqa: E402


def ok(message):
    return check_commit_message(message, "test") == []


def test_accepts_a_normal_subject():
    assert ok("feat(ingest): add entsoe client with retry and rate limiting")


def test_accepts_body_after_blank_line():
    assert ok(
        "fix(ingest): handle 25-hour dst boundary\n\n"
        "Paris has a 25-hour day in October."
    )


def test_rejects_em_dash():
    assert not ok("feat(lake): add writer — append only")


def test_rejects_emoji():
    assert not ok("feat(lake): add bronze writer \U0001f680")


def test_rejects_banned_word():
    assert not ok("feat(api): add robust retry handling")


def test_rejects_long_subject():
    assert not ok("feat(orchestration): " + "x" * 80)


def test_rejects_unknown_type():
    assert not ok("update: change the thing")


def test_rejects_capitalised_summary():
    assert not ok("feat(lake): Add bronze writer")


def test_allows_acronym_summary():
    assert ok("fix(ingest): DST boundary drops one settlement period")


def test_rejects_attribution_trailer():
    assert not ok("chore: scaffold repository\n\nCo-Authored-By: someone <a@b.c>")


def test_ignores_git_comment_lines():
    assert ok("chore: scaffold repository\n\n# Please enter the commit message")


def test_allows_merge_commits():
    assert ok("Merge branch 'main' into feat/bronze-writer")


def test_prose_check_flags_line_numbers(tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("fine line\nthis one is — broken\n", encoding="utf-8")
    problems = check_prose(doc)
    assert len(problems) == 1
    assert ":2:" in problems[0]
