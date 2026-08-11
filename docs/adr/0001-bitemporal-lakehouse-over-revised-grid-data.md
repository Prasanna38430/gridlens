# ADR-0001: bitemporal lakehouse over revised grid data

Status: accepted
Date: 2026-08-11

## Context

European transmission system operators revise what they publish. ENTSO-E puts
out near-real-time generation and load, then corrects it as metering settles,
sometimes days later, sometimes weeks. RTE does the same for France. This is
not an error in my pipeline. It is the source telling me that what I recorded
earlier is no longer what it believes.

The usual way to handle that is to upsert on a natural key like
`(zone, timestamp)`. It is one line of SQL and it quietly destroys the
history. A carbon intensity figure I reported in March changes in May with no
record that it ever read differently. If a counterparty disputes an invoice, I
cannot reproduce the number I actually sent them, only the number the data
would produce today. For anything used in settlement or in P and L
attribution, that is the difference between an auditable system and a
plausible one.

I also have constraints that shape this. I am paying for it myself, so steady
state has to stay under 5 EUR a month, which rules out anything with an idle
hourly charge. And AWS and Iceberg are both new to me. My background is GCP,
BigQuery, dbt and Airflow, so I am choosing this stack partly because I need
to learn it, and I would rather learn it on a problem where the modelling is
the hard part than on a CRUD pipeline where the cloud is the only novelty.

## Decision

Every bronze row carries two timestamps:

- `valid_time`, the settlement period the measurement describes
- `known_at`, the moment I learned this version of it

Bronze is append-only. A revision is a new row with a later `known_at`, never
an update to an existing one. Nothing above bronze is allowed to collapse
those two clocks into one.

Silver exposes two reads of the same facts. A `current` model, which keeps the
latest `known_at` per `valid_time`, and an `as_of(t)` read, which keeps the
latest `known_at` that is less than or equal to `t`. Gold marts are built on
those, and every gold run writes a manifest recording the git SHA, the Iceberg
snapshot IDs it read, and the emission factor version it applied.

The proof that all of this works is an endpoint, `/restate?as_of=`, that
returns a historical figure as it was known on a given date, plus a nightly
audit DAG that re-runs last month and asserts byte-identical output. If those
two things do not work, the modelling is decoration.

Out of scope, deliberately: Kubernetes, any ML model, managed streaming
(MSK, Kinesis), and managed Airflow. The first two add no signal to the
thesis. The last two break the cost ceiling. Redpanda self-hosted gives the
Kafka API without the bill.

## Consequences

What this buys me. Late-arriving data stops being a special case and becomes
an ordinary append. Reprocessing is a read at a different `known_at` rather
than a rebuild. Two engineers can disagree about a number and settle it by
querying the same table at two different points in time.

What it costs me. Storage grows without bound, because nothing is ever
deleted, so compaction and snapshot expiry stop being housekeeping and become
load-bearing. Every silver model needs a window or a merge on
`(key, known_at)` rather than a plain scan, and Athena bills per byte scanned,
so the `as_of` path is going to be the expensive one. I do not yet know
whether a single partition and sort layout serves both the `current` read and
the `as_of` read well, and I expect to find out the hard way. If it turns out
one layout cannot serve both, the fallback is a materialised daily snapshot
table, which costs more storage and is less honest, and I would record that
reversal as its own ADR.

There is also a project-shaped cost. Weeks 1 and 2 produce infrastructure and
an append-only table with no user-visible output. The thing that makes this
project worth building does not become demonstrable until Week 3. I am
accepting that ordering rather than building a dashboard early to have
something to show.

## Notes

Emission factors from ADEME are themselves versioned, so carbon intensity has
a third dimension of time. I am treating factor version as an attribute
recorded in the run manifest rather than as a third clock in the table.
Revisit if that turns out to be too weak.
