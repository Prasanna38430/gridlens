-- temporary: forces state:modified to select this model so the ci build
-- path is actually exercised. reverted in the next commit.
{{ config(materialized="view") }}

-- What we believe today. One row per series and settlement period.
--
-- A view rather than a table: it is a single predicate over a partitioned
-- table, and materialising it would mean a second copy of nearly the whole
-- dataset to save a filter.

select * from {{ ref('generation_versions') }}
where is_current
