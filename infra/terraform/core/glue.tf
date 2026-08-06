# only bronze exists at this point. silver and gold get their databases when
# dbt lands, so that the catalog never advertises a namespace with no tables.
resource "aws_glue_catalog_database" "bronze" {
  name         = "gridlens_bronze"
  description  = "append-only, one row per revision, valid_time and known_at on every row"
  location_uri = "s3://${aws_s3_bucket.data["lake"].id}/bronze/"
}
