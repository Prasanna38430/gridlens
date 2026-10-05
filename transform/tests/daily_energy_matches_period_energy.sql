-- The daily mart aggregates the period mart, so the two must agree. If they
-- drift, the period length conversion has been applied twice or not at all,
-- which is a factor of four at 15 minute resolution and easy to miss by eye.
--
-- No tolerance. Both sides are exact decimals now, so any difference at all is
-- a real one. This used to allow 1e-6 because both sides were doubles.

with from_periods as (
    select
        zone,
        settlement_day,
        cast(sum(net_mw * resolution_minutes) as decimal(38, 6)) / 60 as energy_mwh
    from {{ ref('period_generation_net') }}
    group by 1, 2
),

from_daily as (
    select zone, settlement_day, sum(energy_mwh) as energy_mwh
    from {{ ref('daily_generation_mix') }}
    group by 1, 2
)

select
    from_periods.zone,
    from_periods.settlement_day,
    from_periods.energy_mwh as period_total,
    from_daily.energy_mwh as daily_total
from from_periods
join from_daily
    on from_daily.zone = from_periods.zone
   and from_daily.settlement_day = from_periods.settlement_day
where from_periods.energy_mwh <> from_daily.energy_mwh
