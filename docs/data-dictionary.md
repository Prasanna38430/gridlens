# Data dictionary

What each layer holds, what each column means, and what the data actually
looks like rather than what the schema permits.

Bronze figures were measured on 2026-09-05 against 48,658 rows covering 32
settlement days, 2026-08-04 to 2026-09-04, one zone. Silver and gold figures
were measured on 2026-10-05, when bronze held 90,031 rows over 62 settlement
days, 2026-08-04 to 2026-10-04.

Bronze table design and the Athena limitations behind it are in
`sql/README.md`. Why bronze is append only is ADR-0004. How to read any layer
as it stood at an earlier moment is in `docs/restatement.md`.

```
gridlens_bronze.generation          one row per version, append only
  -> gridlens_silver.stg_generation       view, renames and derives
  -> gridlens_silver.generation_versions  each version with the interval it was believed in
  -> gridlens_silver.generation_current   view, the newest version of each period
  -> gridlens_gold.period_generation_net  signed MW per type per period
  -> gridlens_gold.daily_generation_mix   MWh and share per type per settlement day
     gridlens_gold.run_manifest           one row per dbt run
```

# Bronze

## The grain

One row is one observation of one production type, in one direction, for one
settlement period, as it was known at one instant.

The identity of a row is six columns:

    source, zone, production_type, direction, valid_time, known_at

Nothing is unique on `(zone, valid_time)` and that is the point. Fetch a period
twice and get two rows. The pair `(valid_time, known_at)` is what makes the
table bitemporal, and dropping `known_at` from a query silently collapses
history.

## Columns

| Column | Type | Null | Meaning |
|---|---|---|---|
| `source` | string | no | Which publisher the row came from. Only `entsoe` so far. |
| `source_document_id` | string | no | The `mRID` of the document it was parsed from. Provenance, not identity. |
| `zone` | string | no | Bidding zone EIC code. Only `10YFR-RTE------C` so far. |
| `production_type` | string | no | ENTSO-E `psrType` code, `B01` to `B25`. |
| `direction` | string | no | `generation` or `consumption`. |
| `unit` | string | no | Always `MW`. Kept because the source states it and dropping it would make that an assumption. |
| `resolution_minutes` | int | no | Period length. Always 15 in the data so far. |
| `valid_time` | timestamp | no | Start of the settlement period, UTC. |
| `known_at` | timestamp | no | When this project learned the value. |
| `source_updated_at` | timestamp | yes | The publisher's own revision stamp, where it gives one. Always null from ENTSO-E. |
| `quantity_mw` | decimal(12,3) | no | The measured value. |

### `valid_time` is UTC, and the type does not enforce it

Athena rejects `timestamp with time zone` in Iceberg DDL, in either SQL
dialect, so the column is a plain `timestamp`. UTC is a convention held up by
the contract layer, which refuses any timestamp that is naive or carries a
non-zero offset, and by tests. It is not held up by the type.

The concrete reason it is UTC rather than Paris local: two aware datetimes in
the same zone compare on the wall clock and ignore `fold`, so the two 02:30s on
the October Sunday compare equal and subtract to zero while being an hour apart
in reality. A dict keyed on local time drops one of them without complaining.

### `known_at` is an input, not a clock reading

EventBridge Scheduler substitutes its scheduled time into the payload, and that
value is identical across every retry of one firing. Reading the clock instead
would make each retry look like a fresh revision. Backfills pass it explicitly
for the same reason.

The visible consequence in the data: scheduled runs carry values like
`2026-08-28 04:30:00.000000`, exactly on the second. Where you see a value with
sub-second precision, that row came from a run that read the clock, which was
every run before 2026-08-28. See the note on the outage in the README.

### `source_updated_at` is not `known_at`

RTE publishes an `updated_date` per value and it is a real source side revision
stamp: on the October DST fixture, 264 of 312 values had been revised two days
after publication. ENTSO-E has no equivalent, so this column is null for every
row currently in the table. It is evidence about the publisher, never the
project's own clock.

## Codes

`production_type` is the ENTSO-E common code list, mapped to names in
`gridlens.contracts.reference.ProductionType`. What is actually present:

| Code | Name | Directions present | Rows | Range MW |
|---|---|---|---|---|
| B01 | Biomass | generation | 3,296 | 111.19 to 325.07 |
| B04 | Gas | generation | 3,348 | 99.73 to 5,094.83 |
| B05 | Hard coal | consumption, and one zero | 3,262 | 0.69 to 6.53 |
| B06 | Oil | generation | 2,891 | 29.41 to 1,072.05 |
| B10 | Hydro pumped storage | both | 6,610 | 0.00 to 3,421.51 |
| B11 | Hydro run of river | generation | 3,437 | 1,609.57 to 3,175.17 |
| B12 | Hydro reservoir | generation | 3,348 | 110.62 to 3,073.53 |
| B14 | Nuclear | generation | 3,346 | 27,574.21 to 39,733.29 |
| B16 | Solar | generation | 2,406 | 0.00 to 22,324.83 |
| B17 | Waste | generation | 3,338 | 381.10 to 470.93 |
| B18 | Wind offshore | generation | 3,347 | 9.29 to 1,944.92 |
| B19 | Wind onshore | generation | 3,348 | 551.59 to 12,015.96 |
| B25 | Energy storage | both | 6,681 | 0.00 to 494.74 |

Thirteen production types, fifteen series, because two of them are
bidirectional.

### Storage appears twice, and summing by type double counts it

Pumped storage and batteries show up in the same document twice, once with
`inBiddingZone_Domain` and once with `outBiddingZone_Domain`. The normalizer
turns that into the `direction` column. Any aggregate over `production_type`
that does not filter or sign `direction` counts stored energy twice, once on
the way in and once on the way out.

### Solar has fewer rows than everything else

2,406 against roughly 3,340. ENTSO-E omits solar positions at night rather
than publishing zeros, so the number of periods a solar series carries tracks
daylight: a flat 70 a day through August, 62 by mid September, and lower still
towards midwinter.

That is why completeness is not checked per series. The first version of the
check did that, with a floor of 70 measured in August, and from the start of
September it failed every morning on days that were entirely complete. Any
fixed floor per series is a number measured in one season and wrong in the
next, and a floor low enough for winter solar would let nuclear fall from 96 to
40 without a word.

Completeness is checked per settlement day instead: does the day, across all
of its series, hold every period the calendar says it has, 96 normally and 92
or 100 at a clock change. Sparsity in one series is a property of the source. A
short day is a property of the fetch, which is the thing this pipeline controls.

The day grain found something the per series floor had hidden. 2026-08-29 was
fetched before ENTSO-E had finished publishing and held 84 periods, which
passed a floor of 70. It has since been backfilled to 96.

What this still cannot tell you is whether, inside the interval the source
declared, every position it should have published is present. That needs the
missing positions the contract gate computes and nothing yet stores.

### Hard coal is consumption, apart from a single zero

B05 is 3,448 `consumption` rows between 0.69 and 6.53 MW, and exactly one
`generation` row, at 2026-09-02 14:00, whose value is 0.000 MW.

France has almost no coal generation left, and what ENTSO-E publishes under
this type for FR looks like auxiliary draw rather than output. The lone
generation position does not contradict that, because it reports zero: the
source emitted the direction, not any output.

Two corrections to an earlier version of this section, both mine. It said
"all consumption", which the single row disproves. And it gave the range as
0.00 to 6.53, where the 0.00 was that generation row leaking into an aggregate
grouped by production type but not by direction. Grouping by type alone is the
same mistake the `is_storage` column exists to prevent elsewhere.

The auxiliary draw reading is still unverified against RTE.

## Partitioning

`zone` as an identity transform, and `day(valid_time)`. Both are hidden
partitions, so a query filtering on `valid_time` prunes without naming a
partition column.

`day(valid_time)` is a **UTC** day, not a settlement day. A French settlement
day runs 22:00 to 22:00 UTC in summer, so one local day touches two partitions.
That is correct and cheap, and it means any completeness check has to group on
the Paris date rather than the UTC date. Grouping on the UTC date splits every
fetch across two dates and reports 8 periods on one and 88 on the next, which
looks like a catastrophe and is not.

Not partitioned by `known_at`, deliberately. That would make "what did we learn
today" cheap and the `as_of` read expensive, and the `as_of` read is the one
this project exists to serve.

## Reading the table as of a past moment

Silver answers this now. `gridlens_silver.generation_versions` carries the
window each version was believed in, as a half open interval:

```sql
SELECT production_type, direction, valid_time, quantity_mw
FROM gridlens_silver.generation_versions
WHERE valid_time >= TIMESTAMP '2026-08-04 00:00:00'
  AND valid_time <  TIMESTAMP '2026-08-05 00:00:00'
  AND known_from <= TIMESTAMP '2026-08-25 00:00:00'
  AND (TIMESTAMP '2026-08-25 00:00:00' < known_to OR known_to IS NULL)
```

Run against that period it returns 2,881.920 MW for run of river at 18:30.
Ask as of 2026-08-27 and it returns 2,881.730. `generation_current` is the
same table filtered to `known_to IS NULL`.

The interval is half open on purpose. Closed on both ends returns two rows for
any key whose version changed at exactly the instant asked about, which on this
table is all 89 genuine revisions.

The literal bound on `valid_time` is still not optional. It is what Athena
prunes on, and the partition spec on the silver table matches bronze for that
reason.

Straight from bronze, without silver, the same question needs a window
function over every version of every period:

```sql
SELECT production_type, direction, valid_time, quantity_mw
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY source, zone, production_type, direction, valid_time
        ORDER BY known_at DESC
    ) AS recency
    FROM gridlens_bronze.generation
    WHERE valid_time >= TIMESTAMP '2026-08-04 00:00:00'
      AND valid_time <  TIMESTAMP '2026-08-05 00:00:00'
      AND known_at  <= TIMESTAMP '2026-08-25 00:00:00'
)
WHERE recency = 1
```

Both return the same answer. The second one does the sorting again on every
call, which is the whole reason the first one exists.

Gold has no interval columns, so a gold figure as it stood earlier means
rebuilding gold with a bound on `known_at`. `docs/restatement.md` has the
commands and a worked example.

## What is not here

Gaps. The contract gate produces them as a third outcome, neither record nor
violation, and nothing persists them. A period missing from this table is
currently indistinguishable from a period the source never published, and
resolving that is a silver decision rather than a bronze one.

Quarantined rows. They go to their own bucket in the same serialisation, and
none exist yet.

RTE. The client and normalizer produce this exact row shape and nothing
schedules them.

# Silver

Built by dbt from bronze. Nothing in silver or gold is maintained in place:
every table is derived, so a full rebuild from bronze reproduces it exactly.
That is what lets the restatement audit compare a rebuild with production and
mean something by it.

## `stg_generation`

A view. One row in, one row out. It renames nothing and adds two columns every
later model needs:

| Column | Type | Meaning |
|---|---|---|
| `settlement_day` | date | The Paris date of `valid_time`. Not the UTC date, see Partitioning above. |
| `is_storage` | boolean | True for `B10` and `B25`, the two types that publish both directions. |

It is also where a restatement happens. Given the variables `restate_as_of`,
`restate_from` and `restate_to`, it filters bronze to what was known at that
moment over that window. On the production target it refuses them.

## `generation_versions`

Every version of every settlement period, with the window it was believed in.
90,031 rows, the same as bronze, because each bronze row is one version.
Iceberg, partitioned by `zone` and `day(valid_time)` like bronze, since the
as_of read filters on `valid_time` first.

The bronze columns carry over, with `known_at` renamed, plus:

| Column | Type | Null | Meaning |
|---|---|---|---|
| `known_from` | timestamp | no | Bronze's `known_at`. Renamed because it is now one end of an interval. |
| `known_to` | timestamp | yes | When the next version of the same period replaced this one. Null while current. |
| `is_current` | boolean | no | `known_to IS NULL`, because it is the commonest filter. |
| `settlement_day` | date | no | From staging. |
| `is_storage` | boolean | no | From staging. |

`known_to` is the next `known_at` for the same `source, zone, production_type,
direction, valid_time`. The interval is half open, `[known_from, known_to)`, so
a value learned at exactly t is the one believed at t.

85,766 rows are current. 4,265 have been superseded, and 89 of those were
superseded by a different value, every one hydro run of river on 2026-08-04.
The other 4,176 were superseded by the same value. They come from backfills
run before the merge learned to skip unchanged rows, on day 11, and they stay:
removing them would mean deleting from a table whose argument is that it never
deletes.

## `generation_current`

A view over `generation_versions` where `is_current`. 85,766 rows, one per
series and period. Not materialised: it is one predicate over a partitioned
table, and a second copy of nearly the whole dataset to save a filter is a bad
trade.

# Gold

## `period_generation_net`

Signed power per production type per settlement period. 74,074 rows.

| Column | Type | Meaning |
|---|---|---|
| `zone` | string | |
| `valid_time` | timestamp | Start of the period, UTC. |
| `settlement_day` | date | The Paris date. |
| `production_type` | string | |
| `is_storage` | boolean | |
| `resolution_minutes` | int | |
| `net_mw` | decimal(38,3) | Generation minus consumption for this type in this period. |

Consumption is negated here, once, so nothing summing this table can count
storage twice. 12,222 periods net negative: hard coal in 5,661, which only ever
draws, batteries in 3,573 and pumped storage in 2,923 while they charge, and
offshore wind in 65.

## `daily_generation_mix`

Energy per production type per settlement day. 806 rows, thirteen types over
62 days.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `zone` | string | no | |
| `settlement_day` | date | no | |
| `production_type` | string | no | |
| `is_storage` | boolean | no | |
| `energy_mwh` | decimal(38,6) | no | Net energy over the day. |
| `gross_mwh` | decimal(38,6) | no | Sum of the types that net positive over the day. |
| `share_of_gross` | double | yes | `energy_mwh / gross_mwh`, null where the type nets negative. |

Energy, not power. MW summed over a day at 15 minute resolution is wrong by a
factor of four, and it is the commonest way to publish a wrong number about a
grid. `energy_mwh` is the sum of `net_mw * resolution_minutes`, taken in exact
decimals and divided by 60 once.

Exact on purpose. It used to be a floating point sum, and two builds of the
same input disagreed on 299 of 364 August rows. `docs/restatement.md` has the
story. `share_of_gross` is still a double, because one division of two exact
decimals rounds the same way every time.

144 shares are null: hard coal on all 62 days, batteries on 56 and pumped
storage on 26, the days each absorbed more than it released. A share of a
total the type did not contribute to means nothing.

## `run_manifest`

One row per production dbt run, appended and never updated. 7 rows.

| Column | Type | Meaning |
|---|---|---|
| `dbt_invocation_id` | string | Unique per run. |
| `run_started_at` | timestamp | |
| `git_sha` | string | The commit that built the run. `unknown` on the 2 rows from before `make dbt` passed it. |
| `factor_version` | string | `none` until emission factors are ingested. |
| `dbt_version` | string | |
| `bronze_snapshot_id` | bigint | The Iceberg snapshot of bronze the run read. |
| `bronze_committed_at` | timestamp | When that snapshot was committed. The restatement audit uses it as its bound. |

The snapshot id is the stronger half. Bronze is append only, so a snapshot id
names an exact set of rows for as long as the snapshot exists, where a
timestamp names whatever had been committed by then.
