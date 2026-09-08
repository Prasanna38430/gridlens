# Terraform owns the databases, dbt owns what goes in them. Splitting it that
# way keeps a model change out of the same review as an iam change.
resource "aws_glue_catalog_database" "bronze" {
  name         = "gridlens_bronze"
  description  = "append-only, one row per revision, valid_time and known_at on every row"
  location_uri = "s3://${aws_s3_bucket.data["lake"].id}/bronze/"
}

# Silver arrived with day 16, gold with day 18. Neither was created before
# there was something to put in it, so the catalog never advertised an empty
# namespace.
resource "aws_glue_catalog_database" "silver" {
  name         = "gridlens_silver"
  description  = "dbt models over bronze, current and as_of reads"
  location_uri = "s3://${aws_s3_bucket.data["lake"].id}/silver/"
}

resource "aws_glue_catalog_database" "gold" {
  name         = "gridlens_gold"
  description  = "settlement grade marts, energy rather than instantaneous power"
  location_uri = "s3://${aws_s3_bucket.data["lake"].id}/gold/"
}
