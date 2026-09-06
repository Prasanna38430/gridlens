{{
    config(
        materialized='incremental',
        incremental_strategy='append',
        table_type='iceberg',
    )
}}

-- One row per dbt run, recording what it read and what built it.
--
-- A gold number is only reproducible if you can say which code produced it and
-- which version of the input it saw. The git sha covers the first. The iceberg
-- snapshot id covers the second, and it is the stronger half: bronze is append
-- only, so a snapshot id names an exact set of rows forever, where a timestamp
-- names whatever happened to be committed by then.
--
-- Append only, like bronze. A manifest that could be updated would be evidence
-- of nothing.

select
    '{{ invocation_id }}' as dbt_invocation_id,
    cast('{{ run_started_at }}' as timestamp) as run_started_at,

    -- passed in by `make dbt`, which reads it from git. A run that does not
    -- set it records unknown rather than pretending to know.
    '{{ env_var("GRIDLENS_GIT_SHA", "unknown") }}' as git_sha,

    -- emission factors are not ingested yet, so this is none rather than a
    -- number nobody published. It becomes meaningful when ADEME lands.
    '{{ env_var("GRIDLENS_FACTOR_VERSION", "none") }}' as factor_version,

    '{{ dbt_version }}' as dbt_version,

    -- read straight from the iceberg metadata table rather than through a
    -- ref. $snapshots is not a dbt relation and cannot be one, and the suffix
    -- has to sit inside the quoted identifier or athena rejects the dollar.
    -- The schema still comes from the source definition rather than a literal.
    (
        select snapshot_id
        from {{ source('bronze', 'generation').schema }}."generation$snapshots"
        order by committed_at desc
        limit 1
    ) as bronze_snapshot_id,

    (
        select cast(max(committed_at) as timestamp)
        from {{ source('bronze', 'generation').schema }}."generation$snapshots"
    ) as bronze_committed_at
