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

Day 14 of 30. Ingestion runs unattended in AWS. At 06:30 Europe/Paris a Lambda
pulls the previous French settlement day from ENTSO-E, validates it against a
contract, quarantines anything that fails, and merges the rest into an
append-only Iceberg table. Around 1,400 rows a day.

Bronze holds 40,351 rows over 26 settlement days, 2026-08-04 to 2026-08-29, in
30 data files totalling 239 KB. Nothing has been quarantined yet, so the
contract has not rejected a row in production.

Bronze never updates. A revision is a new row with a later `known_at`:

    hydro run-of-river, settlement period 2026-08-04 18:30 UTC
      known_at 2026-08-22 09:24:02   2881.920 MW
      known_at 2026-08-26 12:00:00   2881.730 MW

ENTSO-E moved that figure by 0.19 MW and we found out four days later. Both
values are still there, and a query bounded on `known_at` reproduces either.

Across the whole table there are 4,265 second versions, of which 89 changed the
value. Every one of the 89 is hydro run-of-river. The other 4,176 repeat a
value unchanged and are an artifact of backfills run before dedupe existed on
day 11, not of the source revising anything.

Silver, the `as_of` read and the restatement endpoint are still to come, so
answering "what did we think in March" means writing the window function
yourself today.

The first thing the two sources disagreed about is worth stating early. On
26 October 2025, the day the clocks went back, the French day is 25 hours long.
ENTSO-E publishes all of it. RTE publishes 24 hourly values across the 25 hour
window and marks nothing, so an hour of French generation is missing from that
feed with no indication it was ever there.

## Repository rules

CI runs ruff, mypy, pytest, the writing style check, a linux rebuild of the
Lambda bundle, and `terraform plan` against the real account through GitHub
OIDC. No AWS keys exist in the repository or in its secrets.

`main` is covered by a ruleset that blocks deletion, blocks force pushes,
requires linear history and lists the three CI jobs as required status checks.
It does not require pull requests. Everything goes through a short-lived
branch and a PR regardless.

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

### The local stack

Airflow 3.3.1 on Docker Compose, LocalExecutor against Postgres.

    make up
    make password

`make up` generates `.env.docker` if it is missing, then starts four
containers. The UI is on http://localhost:8080 and the user is `admin`, with a
password Airflow generates on first start. `make password` prints it. `make
down` stops everything and keeps the database volume, `make logs` follows the
scheduler.

Measured on an 8 GB machine with WSL2 capped at 2 GB: 916 MiB across all four
containers with a task running. That budget is why every service sits behind a
compose profile, and why Redpanda, Spark and Marquez will get profiles of their
own rather than joining this one.

Three DAGs so far. `bronze_quality` runs the expectation suite every morning
at 07:30 Paris, after the EventBridge ingest has landed, and fails loudly when
an expectation does. `bronze_backfill` runs at 08:00, looks for settlement days
that never arrived in the last week, and asks the backfill Lambda for them.
`smoke` does nothing but import the project inside a task, which is how you
tell a broken container from a broken DAG.

The backfill exists because the same repair was done by hand three times in a
fortnight, twice after a broken scheduler payload and once after ENTSO-E
returned 5xx for three mornings. Its `known_at` is the run's own timestamp
rather than a clock reading, so a retry merges onto the rows the first attempt
wrote instead of inserting the range again as a revision that never happened.

Airflow does not own the ingest. EventBridge still fires that at 06:30, and
replacing a scheduler that works with one that is a day old is how you get a
second outage. Airflow runs the checks that nothing else was running.

Two deliberate choices. Logs live in a named volume rather than a bind mount,
because this repository sits inside OneDrive and pointing a process that writes
log files every few seconds at a syncing folder invites file locks. And `src`
is bind mounted read only onto `PYTHONPATH` rather than installed, so a DAG
imports the same code the tests run against with no rebuild step.

## Infrastructure

Terraform lives in `infra/terraform`, in two stacks. See the README there for
the apply order and what each resource costs.

## Known limitations

Only ENTSO-E is wired up. The RTE client and normalizer work and are tested,
but nothing schedules them and none of their data is in the lake, so the
reconciliation this project argues for cannot run yet.

The contract gate computes gaps, meaning periods the source did not publish,
and nothing stores them. Completeness metrics need that table.

Bronze carries 4,176 redundant revisions from backfills predating dedupe.
Compaction cannot remove them, because it rewrites files without removing rows,
and deleting them would mean a `DELETE` against a table whose whole argument is
that it does not mutate history. They stay.

The Iceberg table has no sort order. Athena's `ALTER TABLE` grammar cannot set
one, so it needs Spark or the Iceberg API. Metadata json is in the same
position: every commit rewrites it with the full snapshot history, Athena
rejects every Iceberg property that would prune it, and it is currently 779 KB
of bookkeeping on 239 KB of data.

The quality suite is not scheduled. It exists, it passes, and running it is a
manual step until Airflow lands.

## What broke

The daily schedule failed silently for two mornings, 2026-08-26 and
2026-08-27. Terraform's `jsonencode` escapes angle brackets, so
`<aws.scheduler.scheduled-time>` reached EventBridge Scheduler in its escaped
form, was never substituted, and arrived at the handler as literal text. The
handler refused it rather than falling back to reading the clock, which was
the right call and the reason nothing worse happened. `terraform plan` was
clean throughout, because the deployed state matched a configuration that was
itself wrong.

Two settlement days went missing and were backfilled at the time we actually
learned them rather than backdated to the runs they missed, so those two days
carry a longer revision lag than their neighbours. That is a true record of an
outage and it stays in the data.

The freshness check in the quality suite would have caught this on the second
morning. It was not scheduled. That is the lesson worth more than the fix.
