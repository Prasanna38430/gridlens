resource "aws_s3_bucket" "data" {
  for_each = local.buckets

  bucket = "gridlens-${each.key}-${local.suffix}"

  tags = {
    purpose = each.value
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  for_each = aws_s3_bucket.data

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  for_each = aws_s3_bucket.data

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# no versioning anywhere here, deliberately. every object is written once
# under a key that already includes known_at, so a second version can only be
# a bug. iceberg does its own snapshotting on top of the lake bucket, and
# s3 versioning would make orphan-file cleanup on day 13 ambiguous: a file
# that iceberg considers unreferenced would still be billable as a version.

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  for_each = aws_s3_bucket.data

  bucket = each.value.id

  # failed multipart uploads are invisible in the console and billed forever.
  # this is the single cheapest lifecycle rule there is.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# no storage-class transitions. entso-e responses are small, and standard-ia
# bills a 128 KB minimum per object plus a per-object transition fee, so
# tiering a few million small files costs more than leaving them in standard.
# revisit once I know the real object size distribution.
