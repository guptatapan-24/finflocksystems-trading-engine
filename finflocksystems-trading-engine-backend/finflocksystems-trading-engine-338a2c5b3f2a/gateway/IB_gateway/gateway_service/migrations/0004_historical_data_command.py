from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gateway_service", "0003_gatewayevent_gateway_event_ack_idx_and_more")]
    operations = [
        migrations.AlterField(
            model_name="gatewaycommand", name="command_type",
            field=models.CharField(choices=[(value, value) for value in [
                "RECONNECT", "SEARCH_CONTRACTS", "QUALIFY", "REQUEST_HISTORICAL_DATA",
                "SUBSCRIBE_MARKET_DATA", "CANCEL_MARKET_DATA", "PLACE_ORDER", "MODIFY_ORDER",
                "CANCEL_ORDER", "KILL_SWITCH", "REFRESH",
            ]], max_length=32),
        ),
    ]
