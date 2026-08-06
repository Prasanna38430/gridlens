.DEFAULT_GOAL := help
.PHONY: help sync hooks fmt lint typecheck test style tf check

# spelled out rather than parsed out of the ## comments, because make on
# windows shells out to cmd.exe and there is no grep or awk there
help:
	@echo sync       create .venv and install from uv.lock
	@echo hooks      install the git hooks
	@echo fmt        format and autofix
	@echo lint       ruff, no autofix
	@echo typecheck  mypy over src and scripts
	@echo test       pytest
	@echo style      writing style check over tracked markdown
	@echo tf         terraform formatting
	@echo check      everything CI runs

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

style: ## writing style check over every tracked markdown file
	uv run python scripts/check_style.py

tf: ## terraform formatting
	terraform fmt -check -recursive infra/terraform

check: lint typecheck test style tf ## everything CI runs
