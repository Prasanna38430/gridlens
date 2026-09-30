-- Every positive contribution divided by the same total has to add to one.
-- A tolerance because each share is a double, and adding a dozen correctly
-- rounded doubles need not land on exactly one. The shares themselves are
-- deterministic, being one division of two exact decimals each.

select
    zone,
    settlement_day,
    sum(share_of_gross) as total_share
from {{ ref('daily_generation_mix') }}
where share_of_gross is not null
group by 1, 2
having abs(sum(share_of_gross) - 1) > 1e-9
