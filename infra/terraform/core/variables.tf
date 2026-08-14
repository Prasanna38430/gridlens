variable "region" {
  type    = string
  default = "eu-west-3"
}

variable "lambda_package" {
  type        = string
  default     = "../../../build/gridlens-ingest.zip"
  description = "built by scripts/build_lambda.py, not committed"
}
