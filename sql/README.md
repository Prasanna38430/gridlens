# Table definitions

Terraform owns the buckets, the catalog database and the Athena workgroup. The
tables themselves are SQL, applied with `scripts/athena.py`. Splitting it that
way keeps a table change out of the same review as an IAM change, and means
adding a column does not require a Terraform apply.

    uv run python scripts/athena.py sql/bronze/generation.sql

DDL is not billed by Athena. Every statement here reports 0 bytes scanned.

## bronze/generation

Append-only. A revision is a new row with a later `known_at`, never an update,
which is why nothing in this table is unique on `(zone, valid_time)`.

### Partitioning: `zone`, `day(valid_time)`

Both are hidden partitions, so a query filtering on `valid_time` prunes without
naming a partition column. That matters more than it sounds: the usual Hive
mistake is a `dt` string column that every query has to remember to filter on,
and forgetting it is a full scan.

`zone` is an identity transform. There are nine of them and queries almost
always name one.

`day(valid_time)` rather than `month` or `hour`. A day of one zone is around
1,400 rows, so a month transform would give partitions too coarse to prune
usefully and an hour transform would give roughly 24 files a day per zone,
which is the small files problem arriving early.

**Not partitioned by `known_at`,** which is the interesting choice. Partitioning
by it would make "what did we learn today" cheap, and that is a real query for
incremental processing. It would make the `as_of` read expensive, because
finding the latest version at or before some instant means reading every
`known_at` partition for the `valid_time` range. The `as_of` read is the one
this project exists to serve, so it wins. If incremental processing later needs
help, the answer is a metadata table of ingested batches rather than
repartitioning bronze.

### `day(valid_time)` is a UTC day, not a settlement day

`valid_time` is UTC, so the partition boundary is UTC midnight. A French
settlement day runs 22:00 to 22:00 UTC in summer, so a query for one local day
touches two partitions. That is correct and cheap. Making the partition follow
the local day would mean storing a derived column and keeping it in step with a
timezone database, which is a much worse trade.

## Writes go through a merge, not an insert

Two filters doing different jobs.

The `WHEN NOT MATCHED THEN INSERT` with no `WHEN MATCHED` clause makes a retry
a no-op. Athena does not even cut a snapshot when a merge inserts nothing.

The subquery in front of it drops staged rows whose value already matches the
newest version held for that key. Without it the daily run appends 1,400
identical rows every morning and `known_at` comes to mean "when we last looked"
rather than "when this value appeared".

Both the merge and the lookback are bounded on `t.valid_time` with a literal.
The join predicate on its own gives Athena no constant to prune on, so a merge
for one day would read every day in the table hunting for matches.

## What Athena cannot do here, tested rather than assumed

**No timezone-aware timestamps.** `timestamp with time zone` and
`timestamp(6) with time zone` are both rejected in Iceberg DDL, in either SQL
dialect. Iceberg gets `timestamp` without a zone, so UTC is a convention rather
than a type guarantee. The convention is enforced upstream instead: the
contract layer refuses any timestamp that is naive or carries a non-zero
offset, and there are tests for it. Worth rechecking on Day 31, when four
engines read the same table and any disagreement about zone handling will
surface.

**No sort order.** Iceberg supports one, and it would help, because rows
arriving in `known_at` order are scattered across `valid_time`. Athena's
`ALTER TABLE` grammar accepts only ADD, DROP, EXECUTE, RENAME and SET, so
`WRITE ORDERED BY` is not available. The metadata confirms it: `sort-orders`
is `[{"order-id": 0, "fields": []}]`. Setting it needs the Iceberg API or
Spark, so it belongs with the writer rather than here.

## What Athena chose on its own

Format version 2, so row-level deletes and merge-on-read are available even
though bronze never uses them.

`write.object-storage.enabled = true`, which puts a hash prefix in each data
file path. S3 partitions its keyspace by prefix, so a million files under one
sequential prefix throttle where the same files spread across hashed prefixes
do not. It also means the physical layout does not mirror the partition values,
which is startling the first time you list the bucket.

## Quality checks

`scripts/validate_bronze.py` runs the expectation suite in
`gridlens.quality.bronze`. It exits non-zero on any failure, so Airflow can
treat it as a task.

The contract gate already checks every row as it arrives, so this deliberately
does not repeat that. It checks what a single batch cannot see:

- duplicate bitemporal keys, which the merge is meant to make impossible, so a
  failure here means the writer is broken rather than the data
- periods older than now, and values known before the period they describe
- freshness, measured as hours since the newest `known_at`
- periods per settlement day, per series

Completeness is grained on the **settlement day**, not the UTC date. A Paris
day runs 22:00Z to 22:00Z, so grouping on the UTC date splits every fetch
across two dates and reports 8 periods on one and 88 on the next. The first
version of this check did exactly that and looked like a catastrophe.

The bounds are measured rather than assumed. A complete day sits between 70
and 96 periods: 96 is a full day at PT15M, and 70 is solar, which has no
positions at night because ENTSO-E omits them rather than sending zeros.

## Table maintenance

Bronze appends about 1,400 rows every morning across two partitions, because a
Paris settlement day straddles UTC midnight. Each merge writes one file per
partition it touches, and each one cuts a snapshot, a manifest and a manifest
list, and rewrites the whole metadata json. Before the first compaction the
table held 36,347 rows in 55 data files averaging 5,126 bytes, and the metadata
describing them came to 3.4 times the size of the parquet itself. The metadata
json alone had grown from 2,418 bytes at the first commit to 32,227 by the
twenty-eighth, because every commit rewrites it with the full snapshot history.
That growth is quadratic in the number of commits, and it is the real argument
for maintenance on a table this small. The small files matter too, but only
because query planning reads every manifest entry before it reads any data.

`sql/maintenance/optimize_generation.sql` runs a bin-pack rewrite:

    uv run python scripts/athena.py sql/maintenance/optimize_generation.sql

The first run took 55 data files down to 24, one per partition, with the mean
file size going from 5,126 to 8,625 bytes. Query results did not move. I
checksummed every row before and after, `FF2F26B4A9F5E4BB` both times, on
36,347 rows summing to 131,494,126.350 MW.

### Compaction is also what applies a delete

Bronze is append only by design, and it still ended up with a delete file. A
row written on day 11 to test the revision path was removed with `DELETE` on
2026-08-26. Athena resolved that as merge-on-read: it wrote a positional delete
file pointing at row 0 of a data file and left the data file alone. The row was
masked from every query, and it was still sitting in the parquet.

Compaction is what made the deletion real. `OPTIMIZE` rewrote the file without
that row and dropped the delete file, so the row count held in data files went
from 36,348 to 36,347 while the number of rows a query returns did not change.

Athena gives no direct way to see this coming. `$files` lists data files only,
and `$delete_files`, `$position_deletes` and `$all_files` do not exist in
Athena at all. The only signal from SQL is that `sum(record_count)` over
`$files` disagrees with `count(*)` by the number of masked rows.
`scripts/table_stats.py` reports both numbers side by side for that reason, and
reads the delete file counts out of the snapshot summary in the metadata json,
which is the only place they are exposed.

### Why it is bounded, and why storage goes up first

The `WHERE` clause is not decoration. I checked that it restricts the rewrite
rather than being accepted and ignored: with a bound of `2026-08-20` on a
deliberately fragmented copy, the seven partitions in range went from 19 files
to 7 and the seventeen out of range stayed at 51. A relative bound works the
same way, so the file can be static and a scheduler can just run it.

Seven days covers the daily append plus the revisions that arrive soon after.
Revisions that land weeks later will refragment an old partition, so a full
rewrite is worth running occasionally. There is no point doing it nightly.

One thing to expect: compaction on its own makes storage worse. The old files
stay on S3, held by the snapshots that still reference them, so after the first
run the table had 78 parquet objects instead of 55 and the manifests doubled to
61. Nothing is reclaimed until those snapshots expire. That is the next step
rather than a defect, and it means the test row is out of the live table but
its bytes are still on disk.

### Snapshot expiry

Compaction hands the storage problem to expiry. `VACUUM` drops snapshots past
the retention policy and deletes the files they were the last to reference.

    uv run python scripts/athena.py sql/maintenance/expire_snapshots.sql

Retention comes from two table properties, set in
`sql/maintenance/retention_generation.sql` and repeated in the `CREATE TABLE`
so a table built from scratch inherits them. A `VACUUM` run before I set them
reclaimed nothing at all, because every snapshot was newer than whatever the
default age is. That is worth knowing: `VACUUM` returning success tells you
nothing about whether it freed anything.

On a fragmented copy of bronze the effect was blunt. Nine snapshots became one,
157 parquet objects became 32, which is exactly the number of live data files,
and the bytes under the table went from 1,045,205 to 334,767. Row count did not
move.

I chose a week, with a floor of five snapshots. The argument is that snapshot
time travel is an operational undo here, not the audit mechanism. What makes a
figure reproducible in this project is `known_at` on the row, which survives
compaction, expiry, and a rebuild of the table from the raw XML in the raw
bucket. Keeping ninety days of snapshots would multiply the metadata footprint
to protect a capability the design deliberately does not lean on. If that
reasoning is wrong, the failure shows up on day 20, when the restatement audit
re-runs last month and has to reproduce it exactly.

Athena spells the properties `vacuum_max_snapshot_age_seconds` and
`vacuum_min_snapshots_to_keep`, and stores them as
`history.expire.max-snapshot-age-ms` and
`history.expire.min-snapshots-to-keep`. They are aliases for the spec
properties, so Spark reads the same policy rather than a second one.

### What expiry does not reclaim

Metadata json files. Every commit writes a new one carrying the whole snapshot
history, and `VACUUM` adds two of its own rather than removing any. Iceberg has
properties for this, and Athena rejects all of them:
`write.metadata.previous-versions-max`,
`write.metadata.delete-after-commit.enabled`,
`vacuum_max_metadata_file_age_seconds` and `write.target-file-size-bytes` each
come back as `Unsupported table property key`. So the largest single class of
metadata on this table grows without a cap that Athena can set, and pruning it
needs the Iceberg API or Spark. That puts it in the same bucket as the missing
sort order: a real limitation of driving Iceberg through Athena alone, not
something to paper over.
