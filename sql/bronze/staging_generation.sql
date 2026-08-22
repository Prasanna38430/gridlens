-- Landing table for one ingest batch, all columns string so the cast happens
-- in the INSERT where a bad value fails loudly instead of becoming null.
--
-- projection.batch_id.type = injected means athena builds the s3 path from the
-- value in the WHERE clause. There is no partition metadata to add, no MSCK to
-- forget, and a query that omits batch_id fails rather than scanning every
-- batch ever staged.
CREATE EXTERNAL TABLE IF NOT EXISTS gridlens_bronze.staging_generation (
    source             string,
    source_document_id string,
    zone               string,
    production_type    string,
    direction          string,
    unit               string,
    resolution_minutes string,
    valid_time         string,
    known_at           string,
    source_updated_at  string,
    quantity_mw        string
)
PARTITIONED BY (batch_id string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://gridlens-raw-054129814335/staging/generation/'
TBLPROPERTIES (
    'projection.enabled' = 'true',
    'projection.batch_id.type' = 'injected',
    'storage.location.template' = 's3://gridlens-raw-054129814335/staging/generation/batch_id=${batch_id}'
)
