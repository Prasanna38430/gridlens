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
