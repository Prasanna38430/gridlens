{{
    config(
        materialized='table',
        table_type='iceberg',
        partitioned_by=['zone', 'day(valid_time)'],
    )
}}

-- Net generation per production type per settlement period, signed.
--
-- Storage is published twice, once flowing out of the grid and once into it,
-- and the two rows carry the same production_type. Summing quantity_mw over
-- production_type therefore counts stored energy on the way in and again on
-- the way out. This mart exists so that a downstream consumer cannot make that
-- mistake: consumption is negated here, once, and everything after sums.
--
-- Reading generation_current rather than generation_versions on purpose. This
-- is what we believe today. The as_of variant of this question is answered by
-- filtering the versions table, not by a second mart.

select
    zone,
    valid_time,
    settlement_day,
    production_type,
    is_storage,
    resolution_minutes,

    sum(
        case
            when direction = 'generation' then quantity_mw
            when direction = 'consumption' then -quantity_mw
        end
    ) as net_mw

from {{ ref('generation_current') }}
group by 1, 2, 3, 4, 5, 6
