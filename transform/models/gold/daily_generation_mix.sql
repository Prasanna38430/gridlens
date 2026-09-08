{{
    config(
        materialized='table',
        table_type='iceberg',
        partitioned_by=['zone'],
    )
}}

-- Energy per production type per settlement day, in MWh.
--
-- Power is not energy. A period carries an average MW over its own length, so
-- MWh is net_mw times the period length in hours. Reporting MW summed over a
-- day is the commonest way to publish a number that is wrong by a factor of
-- four at 15 minute resolution.
--
-- The share is against gross generation, meaning the sum of the types that net
-- positive over the day. A type that nets negative, which is storage on a day
-- it absorbed more than it released, gets a null share rather than a negative
-- one, because a share of a total it did not contribute to is not meaningful.

with energy as (

    select
        zone,
        settlement_day,
        production_type,
        is_storage,
        sum(net_mw * resolution_minutes / 60.0) as energy_mwh
    from {{ ref('period_generation_net') }}
    group by 1, 2, 3, 4

),

totals as (

    select
        zone,
        settlement_day,
        sum(case when energy_mwh > 0 then energy_mwh end) as gross_mwh
    from energy
    group by 1, 2

)

select
    energy.zone,
    energy.settlement_day,
    energy.production_type,
    energy.is_storage,
    energy.energy_mwh,
    totals.gross_mwh,
    case
        when energy.energy_mwh > 0 then energy.energy_mwh / totals.gross_mwh
    end as share_of_gross

from energy
join totals
    on totals.zone = energy.zone
   and totals.settlement_day = energy.settlement_day
