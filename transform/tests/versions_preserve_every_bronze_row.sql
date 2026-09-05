-- Deriving intervals must not add or lose a row. If this fails the window
-- function has changed the grain, which would make every as_of read wrong in
-- a way no single row inspection would reveal.

with counts as (
    select
        (select count(*) from {{ ref('stg_generation') }}) as staged,
        (select count(*) from {{ ref('generation_versions') }}) as versions
)

select * from counts where staged <> versions
