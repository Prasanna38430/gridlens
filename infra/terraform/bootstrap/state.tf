# depends_on so the budget exists before the first billable resource does.
# terraform is free to order unrelated resources however it likes otherwise.
resource "aws_s3_bucket" "state" {
  bucket = "gridlens-tfstate-${data.aws_caller_identity.current.account_id}"

  depends_on = [aws_budgets_budget.monthly]
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  # sse-s3 rather than sse-kms. kms bills per request and terraform reads
  # state on every plan, so kms here buys nothing and costs on every run.
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # versioning has to be on before a noncurrent-version rule means anything
  depends_on = [aws_s3_bucket_versioning.state]
}
