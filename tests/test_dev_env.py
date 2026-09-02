from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dev_env import KEYS, generate, render, write_if_missing  # noqa: E402


def test_keys_are_32_bytes_url_safe_base64():
    for value in generate().values():
        assert len(base64.urlsafe_b64decode(value)) == 32


def test_two_calls_do_not_produce_the_same_secret():
    assert generate() != generate()


def test_every_key_reaches_the_file(tmp_path: Path):
    target = tmp_path / ".env.docker"
    assert write_if_missing(target) is True
    body = target.read_text(encoding="utf-8")
    for name in KEYS:
        assert f"{name}=" in body


def test_an_existing_file_is_never_overwritten(tmp_path: Path):
    # rotating these silently would invalidate every connection airflow has
    # encrypted with the old fernet key.
    target = tmp_path / ".env.docker"
    target.write_text("AIRFLOW_FERNET_KEY=keepme\n", encoding="utf-8")
    assert write_if_missing(target) is False
    assert target.read_text(encoding="utf-8") == "AIRFLOW_FERNET_KEY=keepme\n"


def test_the_file_says_where_it_came_from():
    assert "scripts/dev_env.py" in render(generate())
