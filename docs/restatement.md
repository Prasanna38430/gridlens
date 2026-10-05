# Restatement

A restatement answers one question: what did this number say at some earlier
moment? A counterparty disputes an invoice from three months ago and wants the
figure as it was known then, not as the data reads today. That is the reason
every row in this project carries two clocks, and this page is how the second
one gets used.

Every query and command below was run as written on 2026-10-05, and the output
shown is what came back.

## The example: an hour learned two weeks late

Settlement day 2026-09-18 was fetched on the morning of 09-19 with four
quarter hours missing, 07:00 to 07:45 Paris. The day held 92 periods instead
of 96. The quality suite caught it as a completeness failure. I re-fetched the
day on 2026-10-05 and the missing hour came back: 57 rows, stamped with the
`known_at` of that re-fetch. The 1,307 rows already held were identical and
the merge skipped them.

So for two weeks the true figure for that day was not the figure gold
reported. Both figures are still recoverable, and that is the point.

## Reading silver as it stood

`gridlens_silver.generation_versions` keeps every version of every period with
the half open interval it was believed in, `[known_from, known_to)`. Asking
"as of t" is a range predicate. Nuclear on 2026-09-18, as known at two
different moments:

```sql
SELECT
    as_of,
    count(DISTINCT valid_time) AS periods,
    sum(CASE WHEN production_type = 'B14' THEN quantity_mw * resolution_minutes END) / 60 AS nuclear_mwh
FROM gridlens_silver.generation_versions
CROSS JOIN (VALUES TIMESTAMP '2026-10-01 00:00:00', TIMESTAMP '2026-10-05 12:00:00') AS t (as_of)
WHERE valid_time >= TIMESTAMP '2026-09-17 22:00:00'
  AND valid_time <  TIMESTAMP '2026-09-18 22:00:00'
  AND known_from <= as_of
  AND (as_of < known_to OR known_to IS NULL)
GROUP BY as_of
ORDER BY as_of
```

```
as_of                 periods   nuclear_mwh
2026-10-01 00:00:00   92        861705.115
2026-10-05 12:00:00   96        899661.015
```

The `valid_time` bounds are Paris midnight in UTC, 22:00 the evening before in
summer. They are also what Athena prunes partitions on. A predicate on the
Paris date alone gives it no constant and it reads the whole table.

## Rebuilding gold as it stood

Silver can be read as of any moment. Gold cannot: it is materialised for one
moment, the last `make dbt`. To get a gold figure as it stood earlier, rebuild
gold from bronze with a bound on `known_at`. The staging model takes three
variables for that, and the `audit` target sends the result to a schema of its
own:

```bash
export AWS_PROFILE=gridlens
DBT_AUDIT_SCHEMA=gridlens_audit_byhand transform/.venv/Scripts/dbt run \
    --project-dir transform --profiles-dir transform --target audit \
    --exclude run_manifest \
    --vars '{restate_as_of: "2026-09-30 04:30:32.473000", restate_from: "2026-08-31 22:00:00", restate_to: "2026-09-30 22:00:00"}'
```

`transform/.venv/Scripts/dbt` is the Windows layout `make dbt-install`
creates. Elsewhere it is `transform/.venv/bin/dbt`.

The bound here is not arbitrary. It is `bronze_committed_at` from the
`run_manifest` row of the production build on 2026-09-30, so this rebuilds
September exactly as that build saw it. Then compare with production today:

```sql
SELECT r.settlement_day, r.energy_mwh AS as_of_sept_30, p.energy_mwh AS today
FROM gridlens_audit_byhand.daily_generation_mix AS r
JOIN gridlens_gold.daily_generation_mix AS p
  ON p.settlement_day = r.settlement_day
 AND p.production_type = r.production_type
WHERE r.production_type = 'B14'
  AND r.energy_mwh <> p.energy_mwh
```

```
settlement_day   as_of_sept_30    today
2026-09-18       861705.115000    899661.015000
```

One day of thirty differs, by 37,955.900 MWh, which is the hour that arrived
late. Every other day of September is identical.

Drop the schema afterwards:

```bash
uv run python scripts/dbt_teardown.py gridlens_audit_byhand \
    --data-prefix "s3://gridlens-lake-$(aws sts get-caller-identity --query Account --output text)/audit/"
```

The teardown script drops only schemas starting `gridlens_ci_` or
`gridlens_audit_`. A fixed list, not an argument, because a prefix the caller
chooses is no check at all.

The restatement variables are refused on the production target. A restated
build there would replace production gold with one month of it, so the staging
model raises a compiler error, and it does so while dbt parses the project,
before a single query reaches Athena.

### Why `known_at` and not Iceberg time travel

Athena can read bronze at an old snapshot with `FOR VERSION AS OF`. I filter on
`known_at` instead, for two reasons. `known_at` is the clock this project
promises, recorded in the data itself. And snapshots can be expired by
maintenance, where a row's `known_at` lasts as long as the row.

The two clocks only agree while every row's `known_at` precedes its commit.
`known_at` is an input, so a backfill committed after a build but stamped
before it would break that. The audit checks it rather than assuming it.

## The daily audit

`restatement_audit` runs at 09:00 Paris. It rebuilds the previous calendar
month the way the section above does, then compares the result with
production. In order:

1. Takes the window as every Paris settlement day of last month. Cut at UTC
   midnight instead, the month would start and end with a partial day, and
   both would differ from production for reasons that have nothing to do with
   reproducibility. October comes out an hour longer and March an hour
   shorter.
2. Reads the bound from the newest `run_manifest` row.
3. Checks the clocks: bronze at the snapshot production read, against bronze
   filtered on `known_at` at the bound. `EXCEPT` both ways plus row counts. If
   they disagree it stops here, rather than reporting an unexplained
   difference in gold.
4. Rebuilds silver and gold into `gridlens_audit_<run date>`.
5. Compares `generation_versions`, `period_generation_net` and
   `daily_generation_mix` with production, every column, `EXCEPT` both ways
   plus counts. `EXCEPT` alone is a set operation and would not notice a row
   that appears twice on one side.
6. Drops the schema whatever happened, then a last task fails the run if the
   comparison did. Without that last task a failed comparison would end in a
   green run, because Airflow judges a run by its final tasks and teardown
   succeeds. The first run in the container failed in dbt and ended red, which
   is that path working.

On 2026-10-05 it passed for September: 41,207 bronze rows the same by both
clocks, then 41,207 silver rows, 35,540 period rows and 390 daily rows, zero
differences either way.

What a pass proves: gold is reproducible from bronze at the bound production
used. What it does not prove: that the numbers are right. 2026-09-18 was an
hour short for two weeks and the audit passed every time, correctly, because
production and the rebuild were short in the same way.

## Identical means identical

The first audit, run by hand, compared two builds of identical input. 299 of
364 August rows differed.

`daily_generation_mix` summed `net_mw * resolution_minutes / 60.0`. In Athena
`60.0` is a double literal, not a decimal, so every energy figure was a
floating point sum, and floating point addition is not associative. Athena
adds partial sums in parallel, in whatever order its workers finish, so the
last digits moved between runs. Nuclear on 2026-08-28 is exactly 861,845.2275
MWh. The old gold said `861845.2274999998`.

The sum is now taken in MW minutes, which is exact at the source's scale of
three, and divided by 60 once, at a scale of six. `energy_mwh` and `gross_mwh`
are `decimal(38,6)`. With that, the audit compares with plain equality and any
difference is a real one.

`share_of_gross` is still a double. One division of two exact decimals rounds
the same way every time, so it is deterministic, where a decimal quotient would
take the scale of its inputs and round every share to 1e-6. The rule is not "no
doubles". It is "no floating point aggregation". ADR-0006 records the decision
and the options I turned down.

## Where this stops

`known_at` is when this project learned a value, not when ENTSO-E published
it. The daily ingest fetches yesterday once and never looks at that day again,
so a revision ENTSO-E publishes later is only seen if something fetches the day
a second time. Bronze holds 89 genuine revisions, every one of them run of
river on 2026-08-04, and all of them came from backfills. The re-fetch of
2026-09-18 two weeks on found the 1,307 values it already had unchanged.

Production gold is rebuilt by hand, so a figure is only as fresh as the last
`make dbt`. The manifest says which commit and which snapshot it was.

The clock check needs production's snapshot to still exist. Nothing expires
bronze snapshots yet.

Serving this over HTTP, `/restate?as_of=`, is planned and not built.
