-- Append-only bronze. A revision is a new row with a later known_at.
-- Design notes and the Athena limitations behind it are in sql/README.md.

CREATE TABLE IF NOT EXISTS gridlens_bronze.generation (
    source             string,
    source_document_id string,
    zone               string,
    production_type    string,
    direction          string,
    unit               string,
    resolution_minutes int,
    valid_time         timestamp,
    known_at           timestamp,
    source_updated_at  timestamp,
    quantity_mw        decimal(12, 3)
)
PARTITIONED BY (zone, day(valid_time))
LOCATION 's3://gridlens-lake-054129814335/bronze/generation/'
TBLPROPERTIES (
    'table_type' = 'ICEBERG',
    'format' = 'parquet',
    'write_compression' = 'zstd',
    -- retention, so a table created from scratch inherits the same policy the
    -- live one runs under. see sql/maintenance/retention_generation.sql
    'vacuum_max_snapshot_age_seconds' = '604800',
    'vacuum_min_snapshots_to_keep' = '5'
)
