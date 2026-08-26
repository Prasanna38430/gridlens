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
