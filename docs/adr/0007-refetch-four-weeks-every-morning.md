# ADR-0007: re-fetch the last four weeks every morning

Status: accepted, 2026-10-05

## Context

This project exists because ENTSO-E revises figures after publishing them.
Bronze keeps every version, silver turns versions into intervals, and the
restatement audit proves gold can be rebuilt as of any of them. All of that
assumes revisions actually reach bronze.

They mostly did not. The daily run at 06:30 fetches yesterday and never looks
at that day again. A value ENTSO-E corrects a week later was only recorded if
someone happened to backfill that day by hand. After two months bronze held 89
revised values, every one of them run of river on 2026-08-04, and every one of
them from a hand backfill.

On 2026-10-05 I re-fetched the last week through the backfill Lambda to time
it. It found 53 values that had changed since we first fetched them, gas moving
by up to 418 MW and nuclear by 320 MW, one to three days after the fact. A
re-fetch of the 27 days before that found 148 more. By the end of the morning
288 periods carried a genuine revision, spread over 12 settlement days. The
data the project is about had been there all along. We were not asking for it.

## Decision

A second EventBridge schedule fires the backfill Lambda at 05:30 Paris every
morning with

    {"known_at": "<aws.scheduler.scheduled-time>", "refetch_days": 27}

The handler counts back from the Paris date of `known_at`: the day before
yesterday, and the 26 days before that. Yesterday stays with the 06:30 ingest.
The merge already skips any value that matches what bronze holds, so a re-fetch
only adds a row when ENTSO-E changed something.

**Why 27 days.** The only revisions I had seen before this arrived 18 to 22
days after their settlement day. A window shorter than that would have missed
every one. 27 plus yesterday also stays inside the 31 days the backfill accepts.
I do not know how late ENTSO-E can revise. If revisions start showing up at the
far edge of the window, it should grow.

**Why the backfill Lambda.** It already walks a range of days with a
`known_at` it is given, and it has fifteen minutes. The ingest has two, and the
full re-fetch took 105 seconds. Keeping them apart also means a bad re-fetch
cannot stop yesterday's data arriving.

**Why EventBridge and not Airflow.** Airflow runs on my laptop, and a check
that only runs when the laptop is on has already let one problem sit for two
weeks.

**Why 05:30.** An hour before the ingest, so the two never merge into the same
Iceberg table at once. Retries give up after half an hour for the same reason.

**Why `known_at` is the anchor.** It is the scheduled time, the same on every
retry of one firing. A retry counts back to the same days, stamps the same
`known_at`, and merges nothing new. Counting back from the clock instead would
let a retry after midnight shift the window by a day.

## Options I turned down

**Re-fetch inside the 06:30 run.** One function instead of two, but the
timeout would have to grow from two minutes to five, and a failure on day 20
would fail the run that loads yesterday.

**Re-fetch at fixed ages only**, say 2, 7, 14 and 28 days. Four fetches a
morning instead of 27. But revision lag would only be measurable to the nearest
checkpoint, and that lag is one of the numbers the observability work is meant
to show. The difference in cost is a few cents a month.

**Leave it, and backfill by hand when it matters.** That is what the project
was doing, and it is why bronze looked like ENTSO-E almost never revises.

## Consequences

27 ENTSO-E calls a morning, against a limit of 400 a minute. 27 Athena merges,
12.8 MB scanned on the first full run but billed at the 10 MB minimum per
query, so about 8 GB a month, a few cents. A merge that inserts nothing does not
cut an Iceberg snapshot, so quiet mornings add no commits.

Mornings with revisions do. The two re-fetches on 2026-10-05 added 12 snapshots
and 13 data files, and the metadata json went from 3.7 to 4.7 times the size of
the parquet it describes. That growth is the problem ADR-0005 was about, and
compaction and snapshot expiry are still run by hand. With revisions arriving
daily they need a schedule.

`known_at` on a revision is now the morning we noticed it, at most a day after
ENTSO-E published it, for anything inside four weeks. Before, it was whenever
someone thought to look.

Production gold is behind bronze from the morning a revision lands until the
next `make dbt`. The restatement audit is unaffected, because it bounds on the
snapshot production read.

If something else merges into bronze at 05:30, an Iceberg commit can conflict.
Hand backfills should not run then.
