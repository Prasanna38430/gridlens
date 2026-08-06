output "state_bucket" {
  value       = aws_s3_bucket.state.id
  description = "put this in core/backend.hcl"
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "region" {
  value = var.region
}
