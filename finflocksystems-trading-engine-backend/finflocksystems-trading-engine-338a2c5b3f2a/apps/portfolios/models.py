from django.db import models

class TradingPortfolio(models.Model):
    name = models.CharField(max_length=128)
    account = models.ForeignKey("accounts.BrokerAccount", on_delete=models.PROTECT, related_name="portfolios")
    gateway_session = models.ForeignKey(
        "broker_gateway.BrokerGatewaySession",
        on_delete=models.PROTECT,
        related_name="portfolios",
        null=True,
        blank=True,
    )
    cash_buffer_pct = models.DecimalField(max_digits=8, decimal_places=6, default="0.02")
    margin_buffer_pct = models.DecimalField(max_digits=8, decimal_places=6, default="0.10")
    minimum_notional = models.DecimalField(max_digits=20, decimal_places=8, default="10")
    minimum_quantity = models.DecimalField(max_digits=20, decimal_places=8, default="0.00000001")
    minimum_drift = models.DecimalField(max_digits=8, decimal_places=6, default="0.001")
    kill_switch = models.BooleanField(default=False)

    @property
    def execution_mode(self):
        from apps.execution.modes import execution_mode_for_portfolio

        return execution_mode_for_portfolio(self)

class PortfolioPosition(models.Model):
    portfolio = models.ForeignKey(TradingPortfolio, on_delete=models.PROTECT, related_name="positions")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    average_cost = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    realized_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    market_price = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["portfolio", "instrument"], name="unique_portfolio_position")]
        indexes = [models.Index(fields=["portfolio","updated_at"],name="portfolio_position_time_idx")]

class CashLedgerEntry(models.Model):
    portfolio = models.ForeignKey(TradingPortfolio, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    currency = models.CharField(max_length=8)
    kind = models.CharField(max_length=32)
    reference = models.CharField(max_length=128)
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

class PositionLedgerEntry(models.Model):
    portfolio = models.ForeignKey(TradingPortfolio, on_delete=models.PROTECT)
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    quantity_delta = models.DecimalField(max_digits=24, decimal_places=8)
    price = models.DecimalField(max_digits=24, decimal_places=8)
    realized_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    kind = models.CharField(max_length=32)
    reference = models.CharField(max_length=128)
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
