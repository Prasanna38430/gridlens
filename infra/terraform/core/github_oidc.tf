locals {
  github_repo = "Prasanna38430/gridlens"

  # github now stamps immutable numeric ids into the subject claim, so it reads
  # repo:owner@ownerId/repo@repoId:context rather than repo:owner/repo:context.
  # renaming or transferring a repository cannot be used to inherit a trust
  # policy that way. these ids come from the api and never change.
  github_owner_id = 196623752
  github_repo_id  = 1341085416
}

# github's own oidc issuer. no long-lived aws keys live in the repo as a
# result: actions presents a signed token and sts hands back credentials that
# expire in an hour.
resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # aws stopped verifying this against the live certificate chain in 2023, but
  # the argument is still required and this is github's root thumbprint.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
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

    # scoped to one repository. a wildcard here would let any repository on
    # github assume a role in this account, which is the whole reason this was
    # left out on day 2 rather than stubbed with a placeholder.
    # both spellings are listed because the claim format changed under us and
    # matching only one of them is how this failed the first time. either
    # pattern still pins one repository, which is the property that matters.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${local.github_repo}:*",
        "repo:${split("/", local.github_repo)[0]}@${local.github_owner_id}/${split("/", local.github_repo)[1]}@${local.github_repo_id}:*",
      ]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name               = "gridlens-ci-plan"
  description        = "assumed by github actions to run terraform plan, read only"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "github_plan" {
  # plan has to read the current state of everything it manages, so this is
  # broad in breadth and narrow in verb. nothing here can change anything.
  statement {
    sid = "ReadEverythingPlanTouches"

    actions = [
      "s3:GetBucket*",
      "s3:ListBucket*",
      "s3:GetLifecycleConfiguration",
      "s3:GetEncryptionConfiguration",
      "s3:GetAccelerateConfiguration",
      "s3:GetReplicationConfiguration",
      "glue:GetDatabase",
      "glue:GetTable*",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:GetOpenIDConnectProvider",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "lambda:GetFunction*",
      "lambda:ListVersionsByFunction",
      "logs:DescribeLogGroups",
      "logs:ListTagsForResource",
      "scheduler:GetSchedule",
      "budgets:ViewBudget",
      "budgets:DescribeBudget*",
      "sts:GetCallerIdentity",
      "tag:GetResources",
    ]

    resources = ["*"]
  }

  # the lock file is the one write plan needs. without it terraform refuses to
  # read state at all, and dropping use_lockfile to work around that would let
  # a plan run while an apply is halfway through.
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::gridlens-tfstate-${local.suffix}", "arn:aws:s3:::gridlens-tfstate-${local.suffix}/*"]
  }

  statement {
    sid       = "StateLock"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::gridlens-tfstate-${local.suffix}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "github_plan" {
  name   = "gridlens-ci-plan"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_plan.json
}
