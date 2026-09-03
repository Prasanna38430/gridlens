.DEFAULT_GOAL := help
.PHONY: help sync hooks fmt lint typecheck test style tf check env up down logs password

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
	@echo env        write .env.docker if it is missing
	@echo up         start airflow, http://localhost:8080
	@echo down       stop airflow, keeping the database
	@echo logs       follow the scheduler log
	@echo password   print the generated airflow admin password

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

# Every service sits behind a profile. This box has 8 GB and the week 3 and 4
# services cannot all run at once.
COMPOSE = docker compose --env-file .env.docker --profile airflow

env: ## write .env.docker if it is missing
	uv run python scripts/dev_env.py

up: env ## start airflow on http://localhost:8080
	$(COMPOSE) up -d

down: ## stop airflow, keeping the database volume
	$(COMPOSE) down

logs: ## follow the scheduler log
	$(COMPOSE) logs -f airflow-scheduler

password: ## print the generated airflow admin password
	$(COMPOSE) exec airflow-apiserver cat /opt/airflow/simple_auth_manager_passwords.json.generated
