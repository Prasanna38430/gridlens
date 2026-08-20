# gridlens

A bitemporal, replayable lakehouse over European electricity market data.

The problem: ENTSO-E and RTE revise figures after publishing them.
Near-real-time generation and load numbers get corrected days or weeks later.
If you overwrite on ingest, every historical report silently changes and
settlement numbers stop reconciling with the counterparty.

So gridlens keeps two clocks on every row:

- `valid_time`, the settlement period the row describes
- `known_at`, when we learned it

Bronze is append-only. A revision is a new row, never an update. Anything
built on top can be read as it stands today, or as it stood on a given date.

## Status

Day 7 of 30, tagged `v0.1.0`. Ingestion runs in AWS: a Lambda pulls the
previous French settlement day from ENTSO-E at 06:30 Europe/Paris, lands the
raw XML in S3, validates it against the contract, and writes anything that
fails to a quarantine bucket. Roughly 1,400 records a day pass the gate.

Nothing is queryable yet. Bronze, the Iceberg tables and everything above them
land in week 2, so at this point the lake holds raw responses and nothing else.

The first thing the two sources disagreed about is worth stating early. On
26 October 2025, the day the clocks went back, the French day is 25 hours long.
ENTSO-E publishes all of it. RTE publishes 24 hourly values across the 25 hour
window and marks nothing, so an hour of French generation is missing from that
feed with no indication it was ever there.

## Repository rules

CI runs ruff, mypy, pytest, the writing style check, a linux rebuild of the
Lambda bundle, and `terraform plan` against the real account through GitHub
OIDC. No AWS keys exist in the repository or in its secrets.

`main` is covered by a ruleset that blocks deletion, blocks force pushes and
requires linear history. Status checks are listed as required, and it is worth
being exact about what that buys: GitHub evaluates required checks at merge
time, so they gate pull requests and cannot gate a direct push. CI still runs
on every push to `main`, it just reports after the fact rather than before.
Turning that into a real gate means requiring pull requests, which is a
deliberate trade I have not made yet.

## Intended stack

AWS eu-west-3. Apache Iceberg on S3, Glue Data Catalog, Athena, dbt for
silver and gold. Ingestion on Lambda. Streaming on Redpanda into Spark
Structured Streaming. Airflow self-hosted for orchestration. Terraform for
infrastructure.

Steady state has to stay under 5 EUR a month, which rules out NAT gateways,
EC2, RDS, MWAA, MSK and Kinesis.

## Local development

Needs Python 3.12 and uv.

    uv sync
    uv run pytest

`make check` runs what CI runs. `make help` lists the rest.

Accounts, tokens and AWS credentials are in `docs/setup.md`. Start with the
ENTSO-E token, it is the one with a lead time.

## Infrastructure

Terraform lives in `infra/terraform`, in two stacks. See the README there for
the apply order and what each resource costs.

## Known limitations

Nothing is deployed. The Terraform validates but has never been applied, so no
AWS resource described here exists yet and no data has been ingested.
Decisions get recorded in `docs/adr/` as they are made.
