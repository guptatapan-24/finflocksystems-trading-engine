from django.db import models


class Issuer(models.Model):
    """Stable company identity; ticker symbols remain instrument identities."""

    cik = models.CharField(max_length=16, unique=True, null=True, blank=True)
    legal_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    headquarters = models.CharField(max_length=255, blank=True)
    # The research bundle preserves provenance such as
    # "2020 (1853, United Technologies spinoff)", not just a four-digit year.
    founded = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Instrument(models.Model):
    issuer = models.ForeignKey(
        Issuer, on_delete=models.PROTECT, null=True, blank=True, related_name="instruments"
    )
    symbol = models.CharField(max_length=32)
    asset_class = models.CharField(max_length=24, default="STK")
    exchange = models.CharField(max_length=32, default="SMART")
    primary_exchange = models.CharField(max_length=32, blank=True)
    currency = models.CharField(max_length=8, default="USD")
    sector = models.CharField(max_length=64, blank=True)
    multiplier = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    lot_size = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    min_tick = models.DecimalField(max_digits=20, decimal_places=8, default="0.01")
    fractional_support = models.BooleanField(default=False)
    trading_calendar = models.CharField(max_length=64, default="XNYS")
    active = models.BooleanField(default=True)
    tradable = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["symbol", "asset_class", "exchange", "primary_exchange", "currency"], name="unique_instrument")]

class BrokerContract(models.Model):
    instrument = models.OneToOneField(Instrument, on_delete=models.PROTECT, related_name="broker_contract")
    conid = models.BigIntegerField(unique=True)
    primary_exchange = models.CharField(max_length=32, blank=True)
    local_symbol = models.CharField(max_length=64, blank=True)
    description = models.CharField(max_length=255, blank=True)
    qualified_at = models.DateTimeField(null=True, blank=True)


class OptionContract(models.Model):
    class Right(models.TextChoices):
        CALL = "C", "Call"
        PUT = "P", "Put"

    instrument = models.OneToOneField(
        Instrument, on_delete=models.PROTECT, related_name="option_contract"
    )
    underlying = models.ForeignKey(
        Instrument, on_delete=models.PROTECT, null=True, blank=True,
        related_name="listed_options",
    )
    underlying_conid = models.BigIntegerField(null=True, blank=True, db_index=True)
    expiration = models.DateField()
    strike = models.DecimalField(max_digits=24, decimal_places=8)
    right = models.CharField(max_length=1, choices=Right.choices)
    trading_class = models.CharField(max_length=64, blank=True)
    multiplier = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    style = models.CharField(max_length=16, default="UNKNOWN")
    settlement = models.CharField(max_length=16, default="UNKNOWN")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "underlying_conid", "expiration", "strike", "right",
                    "trading_class", "multiplier",
                ],
                name="unique_option_contract_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(multiplier__gt=0),
                name="option_contract_positive_multiplier",
            ),
        ]
        indexes = [
            models.Index(
                fields=["underlying_conid", "expiration", "right", "strike"],
                name="option_chain_lookup_idx",
            ),
        ]


class InstrumentProviderMapping(models.Model):
    STATUSES = [(value, value) for value in ["PENDING", "VERIFIED", "AMBIGUOUS", "UNSUPPORTED", "ERROR"]]
    VERIFICATION_METHODS = [(value, value) for value in ["AUTOMATIC", "MANUAL"]]

    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="provider_mappings")
    provider = models.CharField(max_length=32, default="FINNHUB")
    provider_symbol = models.CharField(max_length=96, blank=True)
    exchange_mic = models.CharField(max_length=16, blank=True)
    provider_exchange = models.CharField(max_length=128, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    isin = models.CharField(max_length=32, blank=True)
    figi = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="PENDING")
    verification_method = models.CharField(max_length=16, choices=VERIFICATION_METHODS, blank=True)
    metadata = models.JSONField(default=dict)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["instrument", "provider"], name="unique_instrument_provider_mapping"),
            models.UniqueConstraint(fields=["provider", "provider_symbol"], condition=models.Q(status="VERIFIED"),
                                    name="unique_verified_provider_symbol"),
        ]
        indexes = [models.Index(fields=["provider", "status", "updated_at"], name="provider_mapping_status_idx")]
