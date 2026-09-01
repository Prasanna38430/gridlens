# Data dictionary

What is in `gridlens_bronze.generation`, what each column means, and what the
data actually looks like rather than what the schema permits. Figures measured
on 2026-08-30 against 40,351 rows covering 26 settlement days, 2026-08-04 to
2026-08-29, one zone.

Table design and the Athena limitations behind it are in `sql/README.md`. Why
the table is append only is ADR-0004.

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
| B01 | Biomass | generation | 2,725 | 111.19 to 325.07 |
| B04 | Gas | generation | 2,772 | 209.27 to 5,094.83 |
| B05 | Hard coal | consumption | 2,716 | 0.69 to 6.53 |
| B06 | Oil | generation | 2,397 | 29.41 to 1,072.05 |
| B10 | Hydro pumped storage | both | 5,471 | 0.00 to 3,421.51 |
| B11 | Hydro run of river | generation | 2,861 | 1,609.57 to 3,175.17 |
| B12 | Hydro reservoir | generation | 2,772 | 110.62 to 3,073.53 |
| B14 | Nuclear | generation | 2,771 | 27,574.21 to 39,733.29 |
| B16 | Solar | generation | 2,022 | 0.00 to 22,324.83 |
| B17 | Waste | generation | 2,766 | 383.91 to 470.93 |
| B18 | Wind offshore | generation | 2,772 | 9.29 to 1,894.48 |
| B19 | Wind onshore | generation | 2,772 | 551.59 to 12,015.96 |
| B25 | Energy storage | both | 5,534 | 0.00 to 494.74 |

Thirteen production types, fifteen series, because two of them are
bidirectional.

### Storage appears twice, and summing by type double counts it

Pumped storage and batteries show up in the same document twice, once with
`inBiddingZone_Domain` and once with `outBiddingZone_Domain`. The normalizer
turns that into the `direction` column. Any aggregate over `production_type`
that does not filter or sign `direction` counts stored energy twice, once on
the way in and once on the way out.

### Solar has fewer rows than everything else

2,022 against roughly 2,770. ENTSO-E omits solar positions at night rather than
publishing zeros, so a solar day carries around 70 periods where nuclear
carries 96. This is why the completeness check accepts a range of 70 to 96
periods per series per settlement day rather than demanding a full grid.

### Hard coal appears only as consumption

B05 has 2,716 rows, all `consumption`, between 0.69 and 6.53 MW. France has
almost no coal generation left, and what ENTSO-E publishes under this type for
FR looks like auxiliary draw rather than output. I have not confirmed that
reading against RTE, so this records what is in the data and flags the
interpretation as unverified.

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

There is no silver model yet, so this is the query:

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

Drop the `known_at` bound and it reads as of now. The literal bound on
`valid_time` is not optional: a join or window predicate alone gives Athena no
constant to prune on and it will read the whole table.

Run as written, that query returns 2,881.920 MW for run of river at 18:30.
Remove the `known_at` bound and the same query returns 2,881.730. The first is
what we believed on 25 August, the second is what we believe now, and both come
out of the same table with no snapshot involved.

## What is not here

Gaps. The contract gate produces them as a third outcome, neither record nor
violation, and nothing persists them. A period missing from this table is
currently indistinguishable from a period the source never published, and
resolving that is a silver decision rather than a bronze one.

Quarantined rows. They go to their own bucket in the same serialisation, and
none exist yet.

RTE. The client and normalizer produce this exact row shape and nothing
schedules them.
