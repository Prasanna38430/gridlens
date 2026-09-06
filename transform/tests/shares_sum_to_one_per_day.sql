-- Every positive contribution divided by the same total has to add to one.
-- A tolerance because these are decimals divided in the warehouse, not
-- because the arithmetic is allowed to be approximately right.

select
    zone,
    settlement_day,
    sum(share_of_gross) as total_share
from {{ ref('daily_generation_mix') }}
where share_of_gross is not null
group by 1, 2
having abs(sum(share_of_gross) - 1) > 1e-9
