-- Snapshot retention. A week of operational rollback, and a floor of five so a
-- quiet week cannot leave the table with one snapshot and no way back.
--
-- Athena spells these its own way and stores them as the spec properties
-- history.expire.max-snapshot-age-ms and history.expire.min-snapshots-to-keep,
-- so an engine other than Athena reads the same policy.

ALTER TABLE gridlens_bronze.generation SET TBLPROPERTIES (
    'vacuum_max_snapshot_age_seconds' = '604800',
    'vacuum_min_snapshots_to_keep' = '5'
)
