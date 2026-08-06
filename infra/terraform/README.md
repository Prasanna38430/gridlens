# Terraform

Two stacks, applied in order. They are separate because the second one keeps
its state in a bucket the first one creates, and a single stack cannot store
its state in a bucket it has not built yet.

## bootstrap

Local state, applied once. Creates the budget and the state bucket, in that
order. The `depends_on` in `state.tf` is what forces the budget to exist before
the first billable resource does.

    cd infra/terraform/bootstrap
    cp example.tfvars terraform.tfvars     # then put your real email in it
    terraform init
    terraform apply

Take the `state_bucket` output and write it into `core/backend.hcl`:

    bucket = "gridlens-tfstate-<account id>"

Then move bootstrap's own state into that bucket, so it is not sitting in a
gitignored file on one laptop:

    # add the same backend block to bootstrap, then
    terraform init -migrate-state -backend-config=../core/backend.hcl

## core

Three S3 buckets, the bronze Glue database, and the ingest IAM role.

    cd infra/terraform/core
    terraform init -backend-config=backend.hcl
    terraform plan
    terraform apply

`backend.hcl` is gitignored because it carries the account id. There is a
`backend.example.hcl` next to it showing the shape.

## Cost

Everything here is either free tier or too small to bill.

| Resource | Monthly |
|---|---|
| One AWS budget | 0, first two per account are free |
| Four empty S3 buckets | 0, S3 bills on stored bytes and requests |
| Glue database | 0, first million catalog objects are free |
| IAM role and policies | 0, IAM is not billed |
| S3 state object plus lock file | fractions of a cent |

The first real cost appears when data lands in the raw bucket, which is Day 6.

There is no DynamoDB lock table. Terraform 1.10 added S3 native locking, so
`use_lockfile = true` in the backend block replaces it. That is one fewer
resource and one fewer thing to pay for.

## Deliberately absent

No VPC, no NAT gateway, no security groups. Nothing in this project runs inside
a VPC, which is the single biggest reason the bill stays near zero. A NAT
gateway alone would be around 32 EUR a month before it moved a single byte.

No KMS customer-managed keys. Buckets use SSE-S3, which is free. KMS bills per
request, and Terraform reads state on every plan.

No S3 versioning on the data buckets. See the comment in `buckets.tf`.

No GitHub Actions OIDC role yet. That needs the repository name to scope the
trust policy to, and there is no remote yet, so it lands on Day 7 with the
rest of CI rather than being stubbed here.

## Status

Written and validated with `terraform validate` against AWS provider 6.58.0.
Never applied. The credentials on this machine were rejected by STS, so no
part of this has been planned or applied against a real account yet.
