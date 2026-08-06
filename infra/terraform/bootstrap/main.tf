terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
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
