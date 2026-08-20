output "buckets" {
  value = { for k, b in aws_s3_bucket.data : k => b.id }
}

output "bronze_database" {
  value = aws_glue_catalog_database.bronze.name
}

output "ingest_role_arn" {
  value = aws_iam_role.ingest.arn
}

output "ci_role_arn" {
  value       = aws_iam_role.github_plan.arn
  description = "set as the AWS_CI_ROLE repository variable in github"
}
