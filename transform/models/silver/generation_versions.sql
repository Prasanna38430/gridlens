{{
    config(
        materialized='table',
        table_type='iceberg',
        partitioned_by=['zone', 'day(valid_time)'],
        table_properties={
            'vacuum_max_snapshot_age_seconds': '604800',
            'vacuum_min_snapshots_to_keep': '5',
        },
    )
}}

-- Every version of every period, with the window it was believed in.
--
-- known_to is the known_at of the next version of the same series and period,
-- or null where this is the newest. Nothing is deleted and nothing is updated:
-- the intervals are derived, so a rebuild from bronze reproduces them exactly.
--
-- The partition spec matches bronze deliberately. The as_of read filters on
-- valid_time first and known_from second, so pruning on valid_time is what
-- keeps it cheap as the table grows.

with versions as (

    select
        source,
        source_document_id,
        zone,
        production_type,
        direction,
        unit,
        resolution_minutes,
        valid_time,
        settlement_day,
        is_storage,
        quantity_mw,
        source_updated_at,

        known_at as known_from,

        lead(known_at) over (
            partition by source, zone, production_type, direction, valid_time
            order by known_at
        ) as known_to

    from {{ ref('stg_generation') }}

)

select
    *,
    known_to is null as is_current
from versions
