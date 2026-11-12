from django.db import models

class BrokerAccount(models.Model):
    account_id = models.CharField(max_length=64, unique=True)
    alias = models.CharField(max_length=128, blank=True)
    base_currency = models.CharField(max_length=8, default="USD")
    net_liquidation = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    available_cash = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    buying_power = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    daily_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    is_reconciled = models.BooleanField(default=False)
    kill_switch = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

