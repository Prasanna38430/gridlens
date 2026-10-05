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

Day 22 of 30. Ingestion runs unattended in AWS. At 06:30 Europe/Paris a Lambda
pulls the previous French settlement day from ENTSO-E, validates it against a
contract, quarantines anything that fails, and merges the rest into an
append-only Iceberg table. Around 1,400 rows a day.

Bronze holds 90,232 rows over 62 settlement days, 2026-08-04 to 2026-10-04. Nothing has been quarantined yet, so the contract has not
rejected a row in production.

Bronze never updates. A revision is a new row with a later `known_at`:

    hydro run-of-river, settlement period 2026-08-04 18:30 UTC
      known_at 2026-08-22 09:24:02   2881.920 MW
      known_at 2026-08-26 12:00:00   2881.730 MW

ENTSO-E moved that figure by 0.19 MW and we found out four days later. Both
values are still there, and a query bounded on `known_at` reproduces either.

Across the whole table there are 4,464 later versions, and 288 of them changed
the value, across all thirteen production types. 199 of those arrived on
2026-10-05, the first morning anything went back and re-fetched recent days.
Before that bronze held 89, all hydro run-of-river on one day. ENTSO-E had been
revising all along and nothing was asking. A schedule now re-fetches the last
four weeks every morning, see ADR-0007. The other 4,176 later versions repeat
a value unchanged and are an artifact of backfills run before dedupe existed
on day 11, not of the source revising anything.

Gold holds two settlement grade marts, `period_generation_net` and
`daily_generation_mix`, plus a `run_manifest` that records the git commit and
the Iceberg snapshot id every dbt run read. Carbon intensity, price signal and
imbalance are not there: each needs a source that is not ingested yet, and a
mart built on numbers nobody published would be worse than no mart.

Silver answers the as_of question now. `generation_versions` carries the
window each version was believed in as a half open interval, so reading the
table as it stood on a past date is a range predicate rather than a window
function over every version. `generation_current` is that table filtered to the
newest version of each period.

Restatement works end to end. Settlement day 2026-09-18 arrived an hour short,
and the hour was fetched again on 2026-10-05. Gold built on 2026-09-30 said
nuclear produced 861,705.115 MWh that day. Gold today says 899,661.015. Both
figures can be rebuilt on demand, and a daily audit checks that last month
rebuilds to exactly what production holds. The queries are in
`docs/restatement.md`. Serving that over HTTP is still to come.

The first thing the two sources disagreed about is worth stating early. On
26 October 2025, the day the clocks went back, the French day is 25 hours long.
ENTSO-E publishes all of it. RTE publishes 24 hourly values across the 25 hour
window and marks nothing, so an hour of French generation is missing from that
feed with no indication it was ever there.

The architecture as it runs is in `docs/architecture.md`, and every table and
column in `docs/data-dictionary.md`.

## Repository rules

CI runs ruff, mypy, pytest, the writing style check, a linux rebuild of the
Lambda bundle, `terraform plan` against the real account through GitHub OIDC,
and a dbt build of whatever models a pull request changes, in a schema of its
own. No AWS keys exist in the repository or in its secrets.

`main` is covered by a ruleset that blocks deletion, blocks force pushes,
requires linear history and requires the `python`, `package` and `terraform`
jobs to pass. The `dbt` job is not required yet. The ruleset does not require
pull requests. Everything goes through a short-lived branch and a PR
regardless.

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

Measured on an 8 GB machine with WSL2 capped at 2 GB: 1,348 MiB across all
four containers at the worst moment so far, on 2026-10-05, with the quality
suite and the restatement audit running at once. The scheduler container,
where tasks run, peaked at 926 MiB of its 1,536 MiB limit. That budget is why
every service sits behind a compose profile, and why Redpanda, Spark and
Marquez will get profiles of their own rather than joining this one.

Four DAGs so far. `bronze_quality` runs the expectation suite every morning
at 07:30 Paris, after the EventBridge ingest has landed, and fails loudly when
an expectation does. `bronze_backfill` runs at 08:00, looks for settlement days
that never arrived in the last week, and asks the backfill Lambda for them.
`smoke` does nothing but import the project inside a task, which is how you
tell a broken container from a broken DAG.

`restatement_audit` runs at 09:00. It rebuilds the previous calendar month
from bronze into a throwaway schema, bounded at what production had learned
when `make dbt` last ran, and compares every silver and gold table with
production row for row. The bound is read from `run_manifest`, and before
anything is rebuilt the DAG checks that bronze filtered on `known_at` holds
exactly the rows of the snapshot production read. The schema is dropped
whatever the outcome.

The first audit, run by hand, failed. 299 of 364 August rows differed between
two builds of identical input, because daily energy was summed in floating
point and the last digits depended on the order Athena's workers added it up.
Gold now sums in exact decimals. On 2026-10-05 September's audit matched on
41,207 silver rows, 35,540 period rows and 390 daily rows, with zero
differences either way.

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
`transform` is mounted read write, but the audit points dbt's `target/` and
`logs/` at `/tmp` inside the container. Sharing `target/` with dbt on the
Windows host crashed the first run: the parse cache keys files by path, and
Windows writes them with backslashes. dbt has a virtualenv of its own in the
image, because installed beside Airflow it would downgrade four packages
Airflow ships.

### The stream

Redpanda and a producer, in a compose profile of their own.

    make down
    make stream-up

The producer polls ENTSO-E for the current settlement day every fifteen
minutes. ENTSO-E publishes it in what look like hourly batches, about half an
hour after each hour, so the stream sees periods arrive through the day and can
see them change, where the daily Lambda only sees the day the morning after. Every poll publishes everything it
fetched, repeats included, to `gridlens.entsoe.generation.v1`, keyed by
series. Refusals go to a quarantine topic. ADR-0008 has the topic design.

Values are Avro, with the schema in Redpanda's schema registry under
`BACKWARD` compatibility. `quantity_mw` is a decimal, not a double, for the
same reason gold is. The registry refuses a change that would break a reader:
tried on 2026-10-05, a new required field and `quantity_mw` as a double were
both refused.

Measured on 2026-10-05: Redpanda 236 MiB, the producer 52 MiB. Redpanda may
grow to the 512 MB it is given, and with Airflow at its own peak that comes
within a few dozen MB of the 2 GB WSL cap, so `make down` first and the two
profiles run one at a time. `make stream-down` keeps the topics.

The stream only exists while this stack is up. Nothing reads it yet: the
Spark consumer is day 23.

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

Production gold is rebuilt by hand with `make dbt`, not on a schedule. The
restatement audit compares against whatever that last build produced, so a
model change merged without a rebuild fails the audit until someone runs it.
The run manifest's git sha is in the audit's log to tell the two apart.

The audit's clock check needs the Iceberg snapshot production read to still
exist. Nothing expires bronze snapshots today. Once something does, a
production build older than the retention window has to be rebuilt before the
audit can pass.

Revisions are re-fetched for four weeks. A value ENTSO-E corrects later than
that is never seen. I do not know yet whether that happens, and if revisions
start turning up at the far edge of the window it should grow.

`bronze_backfill` fills settlement days that are missing entirely. A day that
arrives partly, like 2026-09-18 with 92 of 96 periods, fails the quality suite
and has to be re-fetched by hand.

Airflow runs on a laptop. Its checks and repairs happen only while the laptop is
on and Docker is running, and a failure is visible only to someone who opens
the UI.

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

Settlement day 2026-09-18 arrived on 09-19 with an hour missing, 92 periods of
96. The quality suite was built for exactly this and flagged it. Nobody saw
that for two weeks: Airflow only runs while Docker is up on my laptop, and a
failed task in a UI nobody opens is a log line, not an alert. I found it by
accident on 2026-10-01 while measuring container memory. Re-fetching the day
on 2026-10-05 brought the hour back, and that re-fetch is now the worked
example in `docs/restatement.md`.
