-- The whole point of the current model. More than one current row for a key
-- means the as_of read would return two answers to a question with one.

select
    source,
    zone,
    production_type,
    direction,
    valid_time,
    count(*) as current_rows
from {{ ref('generation_current') }}
group by 1, 2, 3, 4, 5
having count(*) > 1
