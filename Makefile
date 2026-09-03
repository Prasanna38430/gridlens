.DEFAULT_GOAL := help
.PHONY: help sync hooks fmt lint typecheck test style tf check env up down logs password dbt dbt-debug dbt-install

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
	@echo dbt-install create transform/.venv from transform/requirements.txt
	@echo dbt        run the dbt models and their tests
	@echo dbt-debug  check the dbt connection to athena

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

# dbt gets its own environment. dbt-athena depends on boto3-stubs, and having
# those installed alongside the project changes what mypy sees for every boto3
# call. See transform/requirements.txt.
#
# --profiles-dir rather than DBT_PROFILES_DIR, because make on windows shells
# out to cmd.exe and an inline environment variable is not a thing there.
DBT = transform/.venv/Scripts/dbt --project-dir transform --profiles-dir transform

dbt-install: ## create transform/.venv from transform/requirements.txt
	uv venv transform/.venv
	uv pip install --python transform/.venv/Scripts/python.exe -r transform/requirements.txt

dbt: ## run the dbt models and their tests
	$(DBT) build

dbt-debug: ## check the dbt connection to athena
	$(DBT) debug
