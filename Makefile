.DEFAULT_GOAL := help
.PHONY: help sync hooks fmt lint typecheck test style check

help: ## list targets
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "%-12s %s\n", $$1, $$2}'

sync: ## create .venv and install from uv.lock
	uv sync

hooks: ## install the git hooks
	uv run pre-commit install --install-hooks

fmt: ## format and autofix
	uv run ruff format .
	uv run ruff check --fix .

lint: ## ruff, no autofix
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy over src
	uv run mypy

test: ## pytest
	uv run pytest

style: ## writing style check over README and docs
	uv run python scripts/check_style.py

check: lint typecheck test style ## everything CI runs
