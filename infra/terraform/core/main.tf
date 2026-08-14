terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # bucket comes from backend.hcl, which is gitignored because it carries the
  # account id. use_lockfile is s3 native locking, so there is no dynamodb
  # table to pay for.
  backend "s3" {
    key          = "core/terraform.tfstate"
    region       = "eu-west-3"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "gridlens"
      stack      = "core"
      managed_by = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  suffix = data.aws_caller_identity.current.account_id

  # these become s3 tag values, which reject commas. the api calls it
  # InvalidTag and does not say which character it disliked.
  buckets = {
    raw        = "landing zone for api responses as received"
    lake       = "iceberg warehouse"
    quarantine = "records that failed the contract gate"
  }
}
