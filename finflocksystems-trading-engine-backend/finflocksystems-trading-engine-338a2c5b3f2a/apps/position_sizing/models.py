from django.db import models


class PositionSizingPolicy(models.Model):
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="sizing_policies")
    name = models.CharField(max_length=128, default="Default")
    max_loss_fraction = models.DecimalField(max_digits=12, decimal_places=10, default="0.005")
    max_instrument_weight = models.DecimalField(max_digits=12, decimal_places=10, default="0.10")
    max_participation_rate = models.DecimalField(max_digits=12, decimal_places=10, default="0.01")
    max_strategy_exposure = models.DecimalField(max_digits=12, decimal_places=10, default="1")
    max_gross_exposure = models.DecimalField(max_digits=12, decimal_places=10, default="1")
    max_net_exposure = models.DecimalField(max_digits=12, decimal_places=10, default="1")
    minimum_stop_fraction = models.DecimalField(max_digits=12, decimal_places=10, default="0.001")
    calculation_version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)


class PositionSizingDecision(models.Model):
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    policy = models.ForeignKey(PositionSizingPolicy, on_delete=models.PROTECT)
    order_intent = models.OneToOneField("oms.OrderIntent", on_delete=models.PROTECT, null=True, blank=True, related_name="sizing_decision")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    side = models.CharField(max_length=4)
    target_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    risk_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    weight_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    liquidity_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    cash_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    broker_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    approved_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    entry_price = models.DecimalField(max_digits=24, decimal_places=8)
    stop_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    risk_budget = models.DecimalField(max_digits=24, decimal_places=8)
    binding_constraint = models.CharField(max_length=32)
    limits = models.JSONField(default=dict)
    calculation_version = models.PositiveIntegerField(default=1)
    rejected_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
