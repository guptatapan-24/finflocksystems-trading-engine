import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies=[("instruments","0003_exact_contract_identity"),("market_streams","0002_indicatorvalue_is_final_and_more")]
    operations=[
        migrations.CreateModel(name="MarketDataSubscription",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("conid",models.BigIntegerField()),("timeframe",models.CharField(max_length=16)),
            ("state",models.CharField(choices=[(x,x) for x in ["PENDING","SUBSCRIBING","ACTIVE","DEGRADED","ERROR","CANCELLING","INACTIVE"]],default="PENDING",max_length=24)),
            ("consumer_count",models.PositiveIntegerField(default=0)),("required_history_bars",models.PositiveIntegerField(default=0)),
            ("request_id",models.UUIDField(default=uuid.uuid4)),("gateway_command_id",models.BigIntegerField(blank=True,null=True)),
            ("gateway_connection_generation",models.CharField(blank=True,max_length=64)),("requested_at",models.DateTimeField(blank=True,null=True)),
            ("last_event_at",models.DateTimeField(blank=True,null=True)),("last_error",models.TextField(blank=True)),
            ("created_at",models.DateTimeField(auto_now_add=True)),("updated_at",models.DateTimeField(auto_now=True)),
            ("instrument",models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name="market_subscriptions",to="instruments.instrument"))]),
        migrations.AddConstraint(model_name="marketdatasubscription",constraint=models.UniqueConstraint(
            fields=("instrument","timeframe"),name="unique_market_data_subscription")),
    ]
