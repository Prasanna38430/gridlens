-- One row in, one row out. Staging renames and derives, it does not decide.
--
-- The bitemporal question, which version of a period is current and what did
-- we believe on some past date, is deliberately not answered here. That is
-- silver's job, and answering it in a view every downstream model reads
-- through would make the choice invisible.

select
    source,
    source_document_id,
    zone,
    production_type,
    direction,
    unit,
    resolution_minutes,
    valid_time,
    known_at,
    source_updated_at,
    quantity_mw,

    -- a paris settlement day runs 22:00Z to 22:00Z in summer, so grouping on
    -- the utc date splits one fetch across two dates and reports 8 periods on
    -- one and 88 on the next. every completeness question wants this column.
    cast(valid_time at time zone 'Europe/Paris' as date) as settlement_day,

    -- storage moves energy both ways and entso-e publishes it as two series.
    -- naming it here means a downstream model that forgets is a visible
    -- mistake rather than a silent double count.
    production_type in ('B10', 'B25') as is_storage

from {{ source('bronze', 'generation') }}

{#-
    A restatement: bronze as it stood at one past instant, over one window of
    settlement periods. The nightly audit rebuilds last month this way and
    compares the result with production, row for row.

    known_at rather than iceberg time travel, because known_at is the clock
    this project promises, and snapshots are expired after a week. The audit
    checks separately that the two clocks agree.

    Refused on the production target. A restated build there would replace
    production gold with one month of it.
#}
{%- if var('restate_as_of', none) is not none %}
    {%- if target.name == 'prod' %}
        {{ exceptions.raise_compiler_error("restate_as_of is never allowed on the prod target") }}
    {%- endif %}
where known_at <= timestamp '{{ var("restate_as_of") }}'
  and valid_time >= timestamp '{{ var("restate_from") }}'
  and valid_time < timestamp '{{ var("restate_to") }}'
{%- endif %}
