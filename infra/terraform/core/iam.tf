data "aws_iam_policy_document" "ingest_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ingest" {
  # write only. the ingest path has no reason to read back what it wrote, and
  # not granting GetObject means a compromised ingest function cannot
  # exfiltrate history.
  statement {
    sid = "WriteLandingAndQuarantine"

    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]

    resources = [
      "${aws_s3_bucket.data["raw"].arn}/*",
      "${aws_s3_bucket.data["quarantine"].arn}/*",
    ]
  }

  # api tokens live in ssm parameter store standard tier, which is free.
  # decrypt is needed because they are SecureString under the aws-managed key.
  statement {
    sid       = "ReadApiTokens"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.suffix}:parameter/gridlens/*"]
  }

  # An iceberg commit is read-modify-write on the table metadata: you cannot
  # append a snapshot without first reading the one you are appending to. So
  # the lake bucket needs read as well as write, and the write-only property
  # this role had on day 2 now holds only for raw and quarantine. That is a
  # real weakening and ADR-0004 records why it is accepted.
  statement {
    sid = "ReadWriteTheWarehouse"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]

    resources = ["${aws_s3_bucket.data["lake"].arn}/*"]
  }

  # listing the staged batch and the warehouse prefix
  statement {
    sid     = "ListTheBucketsItWritesTo"
    actions = ["s3:ListBucket", "s3:GetBucketLocation"]

    resources = [
      aws_s3_bucket.data["lake"].arn,
      aws_s3_bucket.data["raw"].arn,
    ]
  }

  # the staged batch is read back out of raw by athena, on this role's behalf
  statement {
    sid       = "ReadStagedBatches"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data["raw"].arn}/staging/*"]
  }

  statement {
    sid = "RunTheLoadQuery"

    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetWorkGroup",
    ]

    resources = [aws_athena_workgroup.gridlens.arn]
  }

  # athena writes its result manifest even for an INSERT that returns no rows
  statement {
    sid     = "AthenaResults"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]

    resources = [
      aws_s3_bucket.athena_results.arn,
      "${aws_s3_bucket.athena_results.arn}/*",
    ]
  }

  # an iceberg commit swaps the metadata pointer on the glue table, so the
  # writer updates the catalog entry as well as the files
  statement {
    sid = "CommitToTheCatalog"

    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetTables",
      "glue:UpdateTable",
    ]

    resources = [
      "arn:aws:glue:${var.region}:${local.suffix}:catalog",
      "arn:aws:glue:${var.region}:${local.suffix}:database/${aws_glue_catalog_database.bronze.name}",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${aws_glue_catalog_database.bronze.name}/*",
    ]
  }

  statement {
    sid       = "DecryptSsmParameters"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.region}:${local.suffix}:alias/aws/ssm"]
  }
}

resource "aws_iam_role" "ingest" {
  name               = "gridlens-ingest"
  description        = "assumed by the ingestion lambdas. nothing uses it yet."
  assume_role_policy = data.aws_iam_policy_document.ingest_assume.json
}

resource "aws_iam_role_policy" "ingest" {
  name   = "gridlens-ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
