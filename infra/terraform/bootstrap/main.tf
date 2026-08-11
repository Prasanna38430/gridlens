terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # chicken and egg. this stack creates the bucket its own state lives in, so
  # the first apply ran on local state and was migrated here afterwards with
  #   terraform init -migrate-state -backend-config=../core/backend.hcl
  backend "s3" {
    key          = "bootstrap/terraform.tfstate"
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
      stack      = "bootstrap"
      managed_by = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
