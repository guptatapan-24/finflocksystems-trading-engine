import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    initial=True
    dependencies=[]
    operations=[
        migrations.CreateModel(name="GatewaySession",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("state",models.CharField(default="DISCONNECTED",max_length=32)),("mode",models.CharField(default="paper",max_length=8)),
            ("reconciled",models.BooleanField(default=False)),("connection_owner",models.CharField(blank=True,max_length=128)),
            ("connection_generation",models.UUIDField(default=uuid.uuid4)),("last_callback_at",models.DateTimeField(blank=True,null=True)),
            ("updated_at",models.DateTimeField(auto_now=True))]),
        migrations.CreateModel(name="GatewayCommand",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("command_type",models.CharField(choices=[(x,x) for x in ["RECONNECT","SEARCH_CONTRACTS","QUALIFY","SUBSCRIBE_MARKET_DATA","CANCEL_MARKET_DATA","PLACE_ORDER","MODIFY_ORDER","CANCEL_ORDER","KILL_SWITCH","REFRESH"]],max_length=32)),
            ("idempotency_key",models.CharField(max_length=128,unique=True)),("payload",models.JSONField(default=dict)),
            ("status",models.CharField(default="PENDING",max_length=24)),("result",models.JSONField(default=dict)),
            ("error",models.CharField(blank=True,max_length=1000)),("attempts",models.PositiveIntegerField(default=0)),
            ("created_at",models.DateTimeField(auto_now_add=True)),("updated_at",models.DateTimeField(auto_now=True))]),
        migrations.CreateModel(name="GatewayEvent",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("event_key",models.CharField(max_length=160,unique=True)),("event_type",models.CharField(max_length=64)),
            ("payload",models.JSONField(default=dict)),("acknowledged",models.BooleanField(default=False)),
            ("created_at",models.DateTimeField(auto_now_add=True))]),
        migrations.CreateModel(name="GatewayOrderReference",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("internal_id",models.CharField(max_length=64,unique=True)),("broker_order_id",models.CharField(blank=True,max_length=64)),
            ("permanent_id",models.CharField(blank=True,max_length=64)),("last_status",models.CharField(blank=True,max_length=32)),
            ("updated_at",models.DateTimeField(auto_now=True))]),
        migrations.CreateModel(name="GatewayHealthSnapshot",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("connected",models.BooleanField(default=False)),("reconciled",models.BooleanField(default=False)),
            ("broker_time",models.DateTimeField(blank=True,null=True)),("details",models.JSONField(default=dict)),
            ("created_at",models.DateTimeField(auto_now_add=True))]),
    ]
