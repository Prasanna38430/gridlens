# ADR-0004: bronze appends, and every row carries two clocks

Status: accepted
Date: 2026-08-22

## Context

ADR-0001 claimed that sources revise what they publish and that overwriting
destroys history. By now I have measured it rather than assumed it. One RTE
response for 26 October 2025 carried four distinct `updated_date` values:
twenty four values published the evening before, and **264 of 312 revised two
days later**. That is not an edge case I am designing around. It is the normal
behaviour of the feed, visible in a single response.

So the question for bronze is not whether revisions happen. It is what a
revision does to a row that already exists.

The tempting answer is an upsert on `(zone, production_type, direction,
valid_time)`. One MERGE statement, one row per settlement period, a table that
always reads correctly. It is also the answer that makes this project
pointless: the figure I reported in March would silently become a different
number in May, and I would have no way to reproduce what I actually sent.

## Decision

**Bronze appends. A revision is a new row.** Nothing in the table is unique on
`valid_time`, and nothing is ever updated or deleted. Two timestamps on every
row carry the meaning:

- `valid_time`, the settlement period the measurement describes
- `known_at`, the moment this process learned this version of it

Both are UTC. `known_at` comes from our own clock at fetch time, never from the
payload: ENTSO-E's `createdDateTime` is when the platform rendered the
response, so asking for October 2025 today stamps it with today. A backfill
using that would claim to have known things before it ran.

`source_updated_at` is a third, optional timestamp holding what the source says
about its own revision. RTE publishes it, ENTSO-E does not. It is evidence, not
a clock we order by.

The consequence readers should expect: a query for one settlement period can
return several rows, and picking one is the caller's problem. Silver resolves
it, in two ways, and that is Day 17.

## How it is written, and why that is not Spark

Athena appends a staged batch rather than a writer holding Iceberg open.

The Lambda writes the batch as newline-delimited JSON to a staging prefix, then
runs `INSERT INTO ... SELECT` against an external table over that prefix. The
staging table is all strings and every column is cast in the SELECT, because a
JSON serde inferring types turns a bad value into a null and an explicit cast
turns it into a failure.

The alternatives lost on cost, not on elegance. PyIceberg would let the Lambda
write Iceberg directly, and it drags PyArrow, which is around a hundred
megabytes into a bundle that is currently nine and would push the deploy past
the direct upload limit. Spark writes bronze in the architecture diagram, and
an EMR Serverless run per ingest breaks the five euro ceiling on its own. A
plain `INSERT ... VALUES` cannot carry 1,400 rows past Athena's query size
limit.

The staging table uses partition projection with an injected batch id, so there
is no partition metadata to register and no MSCK to forget. A query that omits
`batch_id` fails rather than quietly scanning every batch ever staged.

Measured: 1,388 rows appended in 1.7 seconds scanning 472 KB, which at five
dollars per terabyte is too small to write down honestly.

## What this costs, stated plainly

**The ingest role is no longer write-only.** On Day 2 I made a point that it
had no `s3:GetObject`, so a compromised ingest function could add garbage and
could not read history back. An Iceberg commit is read-modify-write on the
table metadata: you cannot append a snapshot without reading the one you are
appending to. So the lake bucket now needs read, write and delete, and the
catalog needs `glue:UpdateTable`.

Raw and quarantine are still write-only. The property I was pleased with holds
for two buckets out of three, and I would rather say that than let a policy
diff quietly retire it. Narrowing it further means a separate loader identity
that the ingest function cannot assume, which is worth doing when there is a
second writer and is ceremony while there is one.

**Storage only grows.** Every revision is another row and nothing is ever
removed. At 1,400 rows a day for one zone that is trivial, and it is a curve
that only bends upward. Compaction and snapshot expiry on Day 13 stop being
housekeeping and become the thing that keeps the table readable.

**A retry duplicates.** If the Athena insert succeeds and the Lambda then dies,
EventBridge retries and appends the same batch again, giving two identical rows
with the same `known_at`. Append-only means nothing prevents that.

*Closed on Day 10.* The append became a `MERGE INTO ... WHEN NOT MATCHED THEN
INSERT` keyed on the full bitemporal identity, and `known_at` stopped being a
clock reading. See the update below.

**No sort order.** Rows arrive in `known_at` order and scatter across
`valid_time`, so a range scan touches more files than it needs to. Athena
cannot set an Iceberg sort order, tested on Day 8. It needs the Iceberg API or
Spark, so it waits for a writer that has one.

## What would make me reverse this

Nothing about revisions. If append-only turns out to be unreadable at silver,
the fix is materialising a resolved view on a schedule, not making bronze
mutable. Bronze being the one place that never lies about what arrived is the
foundation the rest of it stands on.


## Update, 2026-08-25: how the retry gap was closed

Two changes, and the second one is the one that actually matters.

The append became `MERGE INTO ... WHEN NOT MATCHED THEN INSERT`, keyed on
`(source, zone, production_type, direction, valid_time, known_at)`. There is
deliberately no `WHEN MATCHED` clause: if a row with that identity is already
present then the correct action is nothing. Athena does not even cut a snapshot
when a merge inserts zero rows, so a rerun leaves no trace at all.

That alone would not have been enough. A merge only recognises a rerun if the
rerun produces the same key, and `known_at` was `datetime.now()`, so every
retry invented a fresh timestamp, matched nothing, and inserted the batch again
looking exactly like a legitimate revision. **`known_at` is now an input.**
EventBridge Scheduler substitutes its scheduled time into the payload, and that
value is identical across every retry of the same firing. The backfill takes it
as an argument. This is the same idea as Airflow's logical date, and it is why
that concept exists rather than every task reading the clock.

The naming here was a trap worth recording. Day 10 in the plan is called atomic
partition overwrite, which is the standard idiom for idempotent batch writes:
the second run replaces the first, so the outcome is the same either way. On a
bitemporal table that idiom is actively wrong. Overwriting the partition for
20 August would delete the version learned on the 22nd, which is the one thing
this table exists to protect. Idempotence here had to mean something narrower:
rerunning *the same run* inserts nothing, while a *different* run still records
a new version.

Measured against the live table: a two day backfill added 2,791 rows, an
identical rerun added zero, and the same range under a new `known_at` added
2,791 again.
