# one budget with several thresholds rather than three budgets. aws gives two
# budgets per account free and charges per budget per day after that, and the
# thresholds are what I actually care about, not the budget objects.
resource "aws_budgets_budget" "monthly" {
  name         = "gridlens-monthly"
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = var.budget_limit
  limit_unit   = var.budget_currency

  dynamic "notification" {
    for_each = var.budget_alert_thresholds
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  # actual spend tells me after the money is gone. the forecast is the one
  # that leaves time to turn something off.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
