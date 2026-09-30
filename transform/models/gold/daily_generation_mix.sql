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
-- Exact decimal arithmetic, deliberately. This model used to divide by 60.0,
-- and in athena 60.0 is a double literal rather than a decimal, so every
-- energy figure was a floating point sum whose last digits depended on the
-- order the workers added it up. The restatement audit caught it on its first
-- run: 299 of 364 August rows differed between two builds of identical input.
-- Nuclear on 2026-08-28 is exactly 861845.2275 MWh and neither build said so.
--
-- So the sum is taken in MW minutes, which is exact at the source's scale of
-- three, and divided by 60 once, at a scale of six. Dividing per row at scale
-- three instead rounds 96 times a day and was measured 0.02 MWh out.
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
        cast(sum(net_mw * resolution_minutes) as decimal(38, 6)) / 60 as energy_mwh
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
        -- a double on purpose, and still deterministic: one division of two
        -- exact decimals rounds the same way every time. a decimal quotient
        -- would take the scale of its inputs and round every share to 1e-6.
        when energy.energy_mwh > 0
            then cast(energy.energy_mwh as double) / cast(totals.gross_mwh as double)
    end as share_of_gross

from energy
join totals
    on totals.zone = energy.zone
   and totals.settlement_day = energy.settlement_day
