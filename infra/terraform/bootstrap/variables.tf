variable "region" {
  type    = string
  default = "eu-west-3"
}

variable "alert_email" {
  type        = string
  description = "where budget alerts go. kept out of the repo, see example.tfvars"
}

variable "budget_limit" {
  type        = string
  default     = "20"
  description = "monthly cost ceiling. the string type is what the budgets api takes"
}

# aws reports cost in the account's billing currency. setting EUR here on a
# USD-billed account does not convert anything, so this stays USD until I have
# confirmed what the account is actually billed in.
variable "budget_currency" {
  type    = string
  default = "USD"
}

# 5, 25 and 100 percent of 20 gives alerts at 1, 5 and 20
variable "budget_alert_thresholds" {
  type    = list(number)
  default = [5, 25, 100]
}
