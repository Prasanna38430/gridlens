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
