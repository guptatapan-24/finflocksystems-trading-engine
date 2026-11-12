from django.db import models

class KillSwitch(models.Model):
    scope = models.CharField(max_length=32)
    scope_id = models.CharField(max_length=64, blank=True)
    enabled = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["scope", "scope_id"], name="unique_kill_switch")]

class RiskCheckResult(models.Model):
    DECISIONS = [(x, x) for x in ["APPROVED", "RESIZED", "HELD", "REJECTED"]]
    order_intent = models.ForeignKey("oms.OrderIntent", on_delete=models.PROTECT, related_name="risk_checks")
    check_name = models.CharField(max_length=64)
    decision = models.CharField(max_length=16, choices=DECISIONS)
    reason = models.CharField(max_length=255)
    requested_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    approved_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class PreTradeRiskPolicy(models.Model):
    portfolio = models.OneToOneField(
        "portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="pre_trade_risk_policy"
    )
    maximum_order_quantity = models.DecimalField(max_digits=24, decimal_places=8, default="100000000")
    maximum_order_notional = models.DecimalField(max_digits=24, decimal_places=8, default="100000")
    estimated_commission_rate = models.DecimalField(max_digits=12, decimal_places=10, default="0.0005")
    estimated_fixed_fee = models.DecimalField(max_digits=24, decimal_places=8, default="1")
    enabled = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class CapitalReservation(models.Model):
    STATUSES = [(value, value) for value in ["ACTIVE", "CONSUMED", "RELEASED"]]
    account = models.ForeignKey("accounts.BrokerAccount", on_delete=models.PROTECT, related_name="capital_reservations")
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="capital_reservations")
    order_intent = models.OneToOneField(
        "oms.OrderIntent", on_delete=models.PROTECT, null=True, blank=True, related_name="capital_reservation"
    )
    reference_type = models.CharField(max_length=32)
    reference_id = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    estimated_fees = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    status = models.CharField(max_length=16, choices=STATUSES, default="ACTIVE")
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "status"], name="capital_res_account_idx")]
