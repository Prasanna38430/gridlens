# ADR-0005: keep one week of snapshots and compact on a bounded window

Status: accepted, 2026-08-30

DRAFT. Claude wrote this one and I have not rewritten it yet. The definition of
done calls for at least five ADRs in my own words, so this needs replacing
before v1.0.0 rather than editing lightly.

## Context

Bronze appends roughly 1,400 rows every morning across two partitions, because
a Paris settlement day straddles UTC midnight. Every merge writes a file per
partition, cuts a snapshot, writes a manifest and a manifest list, and rewrites
the metadata json with the whole snapshot history in it.

After twelve days the table held 36,347 rows in 55 data files averaging 5,126
bytes, and the metadata describing them came to 3.4 times the size of the
parquet. The metadata json had gone from 2,418 bytes at the first commit to
32,227 by the twenty-eighth. That growth is quadratic in commits, not linear,
and it was the larger problem. The small files matter separately, because query
planning reads manifest entries before it reads data.

Iceberg gives three tools: compaction, snapshot expiry and orphan cleanup.
Expiry is the one with a real decision in it, because dropping a snapshot
destroys the ability to read the table as it stood at that moment.

## Decision

Compact on a bounded window, `valid_time >= current_date - INTERVAL '7' DAY`,
rather than rewriting the whole table.

Keep seven days of snapshots with a floor of five, set through Athena's
`vacuum_max_snapshot_age_seconds` and `vacuum_min_snapshots_to_keep`, which are
stored as the spec properties `history.expire.max-snapshot-age-ms` and
`history.expire.min-snapshots-to-keep`.

Do not write an orphan cleanup job. There are no orphans, and the report in
`scripts/table_stats.py` will say so if that changes.

## Why a week is enough

Snapshot time travel is an operational undo here, not the audit mechanism.

The thing that makes a figure reproducible in this project is `known_at` on the
row. Ask what a settlement period looked like on some past date and the answer
comes from filtering rows, not from reading an old snapshot. That property
survives compaction, expiry, and rebuilding the table from the raw XML still
sitting in the raw bucket. It is also the only mechanism that would survive
moving to another engine, which matters for the four engine test.

So snapshots protect against a bad write in the last few days, and a week of
that is enough to notice and roll back. Keeping ninety days would multiply an
already unfavourable metadata footprint to protect a capability the design
deliberately does not lean on.

The test for whether this is wrong is Day 20. The restatement audit re-runs
last month and asserts identical output. If that needs a snapshot older than a
week, this decision is wrong and the audit will say so directly.

## Alternatives rejected

**Keep every snapshot.** Storage is trivial today, so this is tempting. It
loses because metadata json grows with the square of the commit count and
Athena provides no way to prune it, so the cost curve bends the wrong way
exactly as the table gets useful. Cheap now is not the same as cheap later.

**Compact the whole table each night.** Simpler to write and it rewrites 26
partitions to fix the two that changed. The bounded form is one clause longer
and I verified it actually restricts: on a fragmented copy with a bound of
2026-08-20, the seven partitions in range went from 19 files to 7 and the
seventeen outside stayed at 51.

**Set a target file size and let writes stay large.**
`write.target-file-size-bytes` is rejected by Athena as an unsupported table
property key, along with every metadata retention property Iceberg defines.
Not available, rather than not chosen.

**Skip maintenance until it hurts.** This is the honest default for a table
holding 239 KB. It loses because the point of the project is the mechanism, and
because a delete file was sitting in the table masking a row that nothing would
have removed on its own.

## Consequences

Time travel to before the most recent week is gone. If someone asks what the
table looked like three weeks ago, the answer is a query filtered on `known_at`
against the current table, not a snapshot read, and that answer is better
anyway because it does not depend on retention.

Compaction makes storage worse before expiry makes it better. After the first
`OPTIMIZE` the table had 78 parquet objects instead of 55 and the manifests had
doubled, because the old files were still held by snapshots. That is expected
and it means the two steps belong together.

Metadata json is not reclaimed by anything Athena can run. It went from 31
objects to 38 across the maintenance run, and it is now 779 KB of the 835 KB of
metadata sitting on a 239 KB table. Trimming it needs the Iceberg API or Spark,
which puts it with the missing sort order as a limit of driving Iceberg through
Athena alone.

Compaction is also what applies a delete. A row written on Day 11 to test the
revision path had been removed with `DELETE`, and Athena resolved that as
merge-on-read: a positional delete file masked the row and left it in the
parquet. `OPTIMIZE` rewrote the file without it and expiry deleted the file
that held it. Bronze is append only by intent, and it still needed maintenance
to make a deletion real.

The maintenance ran without touching a row. 36,347 rows fingerprinted
identically before and after, `FF2F26B4A9F5E4BB`, summing to 131,494,126.350
MW.
