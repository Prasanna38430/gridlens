# dbt in pull requests. Its own role rather than an extension of
# gridlens-ci-plan, which cannot write anything and should stay that way.
#
# Who can assume it: a pull_request token from this repository, and nothing
# else. A pull request from a fork cannot, and the reason is worth having in
# writing. Fetching an oidc token needs id-token: write. For pull requests from
# forks github turns write permissions into read only. The one setting that
# would send write tokens to fork workflows cannot be enabled on a public
# repository: the api refuses with "not allowed for public repositories".
#
# The workflow file is not the boundary. A pull request brings its own copy of
# it, so an if: guard can be deleted by whoever opens the pull request. This
# trust policy and github's token rules are the boundary.
#
# Cost: a full build measured at 61 queries, 1.3 MB scanned, 160 MB billed
# after athena's 10 MB minimum, about $0.0008. A pull request builds a subset.
# Monthly delta: zero to the cent.

locals {
  ci_schema_prefix = "gridlens_ci_"
  lake_bucket      = aws_s3_bucket.data["lake"].id
  results_bucket   = aws_s3_bucket.athena_results.id
  glue_catalog_arn = "arn:aws:glue:${var.region}:${local.suffix}:catalog"
}

data "aws_iam_policy_document" "github_dbt_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # exact match on pull_request, in both spellings of the subject claim, for
    # the same reason gridlens-ci-plan lists both
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${local.github_repo}:pull_request",
        "repo:${split("/", local.github_repo)[0]}@${local.github_owner_id}/${split("/", local.github_repo)[1]}@${local.github_repo_id}:pull_request",
      ]
    }
  }
}

resource "aws_iam_role" "github_dbt" {
  name                 = "gridlens-ci-dbt"
  description          = "assumed by github actions on pull requests to build dbt into a gridlens_ci_ schema"
  assume_role_policy   = data.aws_iam_policy_document.github_dbt_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_dbt" {
  statement {
    sid = "QueriesInTheProjectWorkgroupOnly"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:GetWorkGroup",
    ]
    resources = [aws_athena_workgroup.gridlens.arn]
  }

  # Read the catalog for sources and for deferring to production relations.
  statement {
    sid = "ReadTheCatalog"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetPartition",
      "glue:GetPartitions",
    ]
    resources = [
      local.glue_catalog_arn,
      "arn:aws:glue:${var.region}:${local.suffix}:database/${aws_glue_catalog_database.bronze.name}",
      "arn:aws:glue:${var.region}:${local.suffix}:database/${aws_glue_catalog_database.silver.name}",
      "arn:aws:glue:${var.region}:${local.suffix}:database/${aws_glue_catalog_database.gold.name}",
      "arn:aws:glue:${var.region}:${local.suffix}:database/${local.ci_schema_prefix}*",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${aws_glue_catalog_database.bronze.name}/*",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${aws_glue_catalog_database.silver.name}/*",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${aws_glue_catalog_database.gold.name}/*",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${local.ci_schema_prefix}*/*",
    ]
  }

  # Create and drop only its own schemas. Glue authorises every resource in the
  # hierarchy, so holding these on the catalog does not reach gridlens_silver:
  # dropping that would also need them on database/gridlens_silver, which is
  # not granted.
  statement {
    sid = "OwnOnlyCiSchemas"
    actions = [
      "glue:CreateDatabase",
      "glue:UpdateDatabase",
      "glue:DeleteDatabase",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:DeleteTable",
      "glue:BatchCreatePartition",
      "glue:BatchDeletePartition",
    ]
    resources = [
      local.glue_catalog_arn,
      "arn:aws:glue:${var.region}:${local.suffix}:database/${local.ci_schema_prefix}*",
      "arn:aws:glue:${var.region}:${local.suffix}:table/${local.ci_schema_prefix}*/*",
    ]
  }

  statement {
    sid       = "ReadProductionData"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${local.lake_bucket}/bronze/*", "arn:aws:s3:::${local.lake_bucket}/silver/*", "arn:aws:s3:::${local.lake_bucket}/gold/*"]
  }

  statement {
    sid       = "WriteOnlyUnderCi"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["arn:aws:s3:::${local.lake_bucket}/ci/*"]
  }

  # TODO: narrow ListBucket with an s3:prefix condition once a real run shows
  # which prefixes athena lists. Listing returns key names only.
  statement {
    sid       = "ListTheLake"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.lake_bucket}"]
  }

  # the workgroup enforces results/ as its output location, and athena writes
  # results with the caller's own permissions
  statement {
    sid       = "QueryResults"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.results_bucket}", "arn:aws:s3:::${local.results_bucket}/results/*", "arn:aws:s3:::${local.results_bucket}/dbt/*"]
  }
}

resource "aws_iam_role_policy" "github_dbt" {
  name   = "gridlens-ci-dbt"
  role   = aws_iam_role.github_dbt.id
  policy = data.aws_iam_policy_document.github_dbt.json
}
