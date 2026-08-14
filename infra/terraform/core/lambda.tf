locals {
  ingest_function = "gridlens-ingest-entsoe"
}

# created here rather than left to lambda. lambda makes the group on first
# invoke with retention set to never expire, and logs you forget about are the
# usual way a free tier account starts costing money.
resource "aws_cloudwatch_log_group" "ingest_entsoe" {
  name              = "/aws/lambda/${local.ingest_function}"
  retention_in_days = 14
}

resource "aws_lambda_function" "ingest_entsoe" {
  function_name = local.ingest_function
  role          = aws_iam_role.ingest.arn
  handler       = "gridlens.handlers.ingest_entsoe.handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]

  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)

  # 190 KB of xml parsed into ~1400 validated records. 512 MB is more about
  # cpu share than memory: lambda scales cpu with it, and a slower function
  # bills for longer, so the cheapest setting is rarely the smallest.
  memory_size = 512
  timeout     = 120

  environment {
    variables = {
      GRIDLENS_RAW_BUCKET         = aws_s3_bucket.data["raw"].id
      GRIDLENS_QUARANTINE_BUCKET  = aws_s3_bucket.data["quarantine"].id
      GRIDLENS_ZONE               = "FR"
      GRIDLENS_ZONE_TZ            = "Europe/Paris"
      GRIDLENS_ENTSOE_TOKEN_PARAM = "/gridlens/entsoe/token"
      GRIDLENS_LOOKBACK_DAYS      = "1"
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest_entsoe]
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # without this any scheduler in any account that guessed the role name
    # could invoke it. the confused deputy is not hypothetical here.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.suffix]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "gridlens-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "gridlens-scheduler-invoke"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.ingest_entsoe.arn
    }]
  })
}

resource "aws_scheduler_schedule" "ingest_entsoe" {
  name = local.ingest_function

  # paris time, not utc. eventbridge scheduler handles the offset itself, so
  # the job keeps running at 06:30 local across both clock changes. the data
  # inside is still utc, which is the whole point of keeping them separate.
  schedule_expression          = "cron(30 6 * * ? *)"
  schedule_expression_timezone = "Europe/Paris"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ingest_entsoe.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      # the handler raises on a rate limit rather than sleeping through it, and
      # entsoe bans a token for about ten minutes. two quick retries are for a
      # transient 5xx, and anything longer is tomorrow's run's problem.
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}
