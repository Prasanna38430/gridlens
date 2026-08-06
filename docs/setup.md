# Setup

Everything this project needs from the outside world, in the order it becomes
blocking. Nothing here is optional except the last section.

| What | Needed by | Lead time | Cost |
|---|---|---|---|
| AWS account and credentials | Day 2 | minutes | free tier |
| ENTSO-E API token | Day 3 | up to a few working days | free |
| RTE Data API application | Day 4 | minutes to days | free |
| GitHub remote and gh CLI | Day 7 | minutes | free |
| WSL2 memory cap | Day 15 | minutes | free |
| Oracle Cloud ARM VM | Day 22, optional | days, often refused | free |
| Snowflake trial | Day 31, optional | minutes | free for 30 days |

ODRE and ADEME need no credentials. ODRE is an open OpenDataSoft endpoint.
ADEME Base Empreinte publishes downloadable factor files. If either turns out
to need a key, this table gets updated.

## 1. AWS

### 1.1 Protect the root user

Sign in to the console as root, open Security credentials, and turn on MFA.
Then stop using root. Everything below runs as a separate identity.

### 1.2 Turn on IAM access to billing

This one is easy to miss and the failure is confusing. Budgets live under the
billing API, and by default no IAM identity can touch it no matter what policy
it holds. Without this step `terraform apply` on the bootstrap stack fails with
an access denied on `aws_budgets_budget`, and the message does not mention this
setting.

It has to be done while signed in as the root user. An IAM user holding
AdministratorAccess cannot change it, which is the reason to do it in the same
sitting as the MFA setup above rather than later.

Top right account menu, Account. Find "IAM user and role access to Billing
information", Edit, tick Activate IAM Access, Update.

While in the billing pages, open Cost Explorer once. Forecasted budget alerts
need historical data to forecast from, so the forecast alert will stay quiet
for the first few weeks whatever you do. The three actual-spend alerts work
immediately.

### 1.3 Create an identity

Two ways. I recommend the first.

**IAM Identity Center.** No long-lived keys on disk, credentials expire on
their own, and it is what most French teams run. Enabling it creates an AWS
Organization for the account if there is not one already, which is fine for a
personal account.

Console, IAM Identity Center, Enable. Pick eu-west-3 when it asks for a region.
Create a user with your email, create a permission set (AdministratorAccess to
start with), and assign the user to the account with that permission set.
Copy the AWS access portal URL it gives you.

Set the permission set session duration to 8 hours. The default is 1, which
means re-authenticating in the middle of every working session.

Then, locally:

    aws configure sso --profile gridlens

It asks for the start URL and the SSO region, opens a browser, and writes the
profile. Set the CLI default region to `eu-west-3` and output to `json`.

Once a day, or whenever a command says the token expired:

    aws sso login --profile gridlens

**IAM user with access keys.** Faster to set up, but the keys sit on disk
forever until you rotate them. Take this path if you want to move today and
switch later.

Console, IAM, Users, Create user. Name it `gridlens`. Attach
AdministratorAccess directly. Open the user, Security credentials, Create
access key, choose Command Line Interface, and copy both values. Then:

    aws configure --profile gridlens

AdministratorAccess is broader than this project needs. I am accepting that for
the account owner running Terraform, because the alternative is spending a day
writing a Terraform-runner policy that I would keep widening anyway. The
identities that matter for least privilege are the ones the pipeline uses, and
those are defined in `infra/terraform/core/iam.tf`.

### 1.4 Use the profile

The Terraform in this repo does not name a profile, so it picks up whatever the
environment gives it. That is deliberate: CI authenticates with OIDC and has no
profile at all.

PowerShell, per session:

    $env:AWS_PROFILE = "gridlens"

Verify:

    aws sts get-caller-identity --profile gridlens

You should see an account id and an ARN. If you get `InvalidClientTokenId`, the
credentials are wrong or expired. There is already a `default` profile on this
machine with dead keys pointing at eu-north-1, so make sure you are not falling
back to it.

### 1.5 Apply the infrastructure

    cd infra/terraform/bootstrap
    terraform apply

`terraform.tfvars` already exists with an alert email in it. Change it if that
is the wrong address, because it is where every budget alert goes.

Read the plan before approving. It should be five resources and no deletions.
Take the `state_bucket` output, then follow `infra/terraform/README.md` for the
state migration and the core stack.

## 2. ENTSO-E Transparency Platform

The main data source, and the one with a real lead time. Start it before
anything else on this page.

Four steps, and registration on its own is not enough. The API entitlement is
granted by a human after you ask for it by email.

1. Go to `transparency.entsoe.eu`, click Sign In, then the Register link at the
   bottom of the dialog. The password rules are stricter than most: long, with
   a special character. Confirm the account through the link that arrives by
   email.
2. Send an email to `transparency@entsoe.eu` with `RESTful API access` as the
   subject and the registered email address in the body. Nothing else is
   needed.
3. Access is granted within three working days and you get a confirmation
   email.
4. Log back in, open My Account, and generate a token. If one already exists
   the page warns you before replacing it, and replacing it invalidates the
   old one.

There is a test environment at `iop-transparency.entsoe.eu` with its own
endpoint at `web-api.tp-iop.entsoe.eu/api`. It holds much less data than
production, so it is only useful for shape-checking a request.

### Limits that shape the client

400 requests per minute, counted **per token, not per IP**. Going over gets the
token temporarily banned for about ten minutes, and the platform returns 429
while that lasts. ENTSO-E suggests throttling to 6 or 7 requests per second on
average with burst handling.

Per token is the detail that matters for the design. Running the ingestion from
several places at once does not buy any headroom, it just reaches the ceiling
faster, so the limiter has to be shared across every caller rather than being
per-process. That is why the Day 3 client fetches zones serially.

### Checking the token works

Production endpoint is `https://web-api.tp.entsoe.eu/api`, https only.

    curl.exe "https://web-api.tp.entsoe.eu/api?securityToken=TOKEN&documentType=A75&processType=A16&in_Domain=10YFR-RTE------C&periodStart=202608050000&periodEnd=202608060000"

Use `curl.exe` and not `curl` in PowerShell. `curl` there is an alias for
`Invoke-WebRequest`, which takes different arguments and will fail confusingly.

That request asks for actual generation per production type for France over one
day. `10YFR-RTE------C` is the EIC code for France, `A75` is generation per
type, `A16` means realised rather than forecast, and the timestamps are UTC in
`yyyyMMddHHmm`.

XML back means it worked. An `AcknowledgementMarketDocument` means the API
rejected the query and the reason code inside says why, so read it rather than
retrying.

## 3. RTE Data API

Register at `data.rte-france.com`. Create an application in your account, then
subscribe the application to each API you want. Subscription is per API, not
per account, which is the part people miss. The application gives you a client
id and a client secret.

RTE uses OAuth2 client credentials. You exchange the pair for a bearer token
that lasts a couple of hours, then call the data endpoints with it. Some APIs
are approved instantly and some are reviewed, so subscribe early.

I have not run this flow myself in this project yet, so treat the details as
unverified until Day 4 proves them.

## 4. GitHub

    winget install GitHub.cli
    gh auth login
    gh repo create gridlens --private --source . --remote origin --push

One thing to decide before Day 7. Branch protection and rulesets on private
repositories are a paid-plan feature on GitHub. The git discipline for this
project says trunk is protected and CI must be green before merge, and on a
free private repo you cannot enforce either. The options are to make the repo
public now and get enforcement for free, or to keep it private and treat the
rules as a personal convention until v1.0.0. Worth checking the current plan
limits rather than trusting this paragraph.

Day 7 also needs a GitHub Actions OIDC role in AWS, which is not written yet
because its trust policy has to name the repository. That is why it was left
out of Day 2 rather than stubbed.

## 5. Secrets

Two places, on purpose.

Locally, a `.env` file in the repository root. It is gitignored. Copy
`.env.example` and fill it in.

In AWS, SSM Parameter Store standard tier, which is free and which the ingest
role in `iam.tf` is already allowed to read:

    aws ssm put-parameter --name /gridlens/entsoe/token --type SecureString --value file://token.txt --region eu-west-3

Use `file://` rather than pasting the value inline. An inline `--value` ends up
in PowerShell's history file in plain text, which then sits on disk indefinitely.
Delete the temporary file afterwards.

The parameter names the code will expect:

    /gridlens/entsoe/token
    /gridlens/rte/client_id
    /gridlens/rte/client_secret

Nothing reads these yet. They get wired up on Day 6 when the Lambda exists.

## 6. Docker and WSL2

Docker Desktop is installed. Before Day 15, cap what WSL2 is allowed to take,
because on 8 GB the default is to let it grow until Windows starts swapping.

Create `C:\Users\<you>\.wslconfig`:

    [wsl2]
    memory=5GB
    processors=4
    swap=2GB

Then `wsl --shutdown` and restart Docker Desktop.

Even with that, the whole self-hosted stack does not fit. Airflow, Redpanda,
Spark, Marquez, Prometheus and Grafana together want more than this machine
has. The compose files will use profiles so that two or three services run at a
time, and the README will say which combinations actually fit.

## 7. Optional

**Oracle Cloud Always Free ARM.** 4 cores and 24 GB, free forever, and often
refused in EU regions for lack of capacity. Apply now and retry, because if it
lands by Week 4 the always-on services move there. If it does not, the
streaming demo runs locally in bounded windows and the README says so.

**Snowflake trial.** Only for the Day 31 stretch goal. Thirty days, no card.
Start it when you get there, not now, or it will have lapsed.
