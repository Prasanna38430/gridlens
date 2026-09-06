-- The interval predicate must partition the known_at axis, not overlap it.
-- Half open intervals are what guarantee this, and closed ones would return
-- two rows for any key whose version changed at exactly the instant asked
-- about. Tested at a revision boundary rather than a quiet moment: bronze
-- holds a revision learned at exactly 2026-08-26 12:00.

{% set moment = "timestamp '2026-08-26 12:00:00'" %}

select
    source,
    zone,
    production_type,
    direction,
    valid_time,
    count(*) as rows_returned
from {{ ref('generation_versions') }}
where {{ known_at(moment) }}
group by 1, 2, 3, 4, 5
having count(*) > 1
