# Query results get their own bucket rather than a prefix in the lake. Day 13
# adds iceberg orphan file cleanup, which walks the warehouse looking for files
# no snapshot references, and athena result sets are exactly that shape. Mixing
# them would mean teaching the cleanup to skip a prefix, and a cleanup with an
# exception is a cleanup nobody trusts.
resource "aws_s3_bucket" "athena_results" {
  bucket = "gridlens-athena-${local.suffix}"

  tags = {
    purpose = "athena query results"
  }
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  # results are a cache of a question already answered. keeping them costs
  # money and answers nothing a rerun would not.
  rule {
    id     = "expire-results"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_athena_workgroup" "gridlens" {
  name = "gridlens"

  configuration {
    # callers cannot override any of this, which is the point. a query that
    # writes its results somewhere else also escapes the scan limit below.
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    # athena bills per byte scanned. a full table scan on a bad predicate is
    # the classic way a pay-per-query lake produces a surprising invoice, so
    # queries are cut off at a gigabyte. at 5 usd per terabyte that caps a
    # single mistake at about half a cent.
    bytes_scanned_cutoff_per_query = 1073741824

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.id}/results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}
