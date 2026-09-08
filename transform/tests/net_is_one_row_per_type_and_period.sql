-- The mart exists to make double counting impossible. Two rows for one type
-- in one period would hand the problem straight back to the consumer.

select zone, valid_time, production_type, count(*) as rows_returned
from {{ ref('period_generation_net') }}
group by 1, 2, 3
having count(*) > 1
