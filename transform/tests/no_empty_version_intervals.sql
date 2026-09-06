-- A version believed for zero time is a contradiction. It happens if two rows
-- share a bitemporal key, which the merge is meant to make impossible, so a
-- failure here means the writer is broken rather than the model.

select
    source,
    zone,
    production_type,
    direction,
    valid_time,
    known_from,
    known_to
from {{ ref('generation_versions') }}
where known_to is not null
  and known_to <= known_from
