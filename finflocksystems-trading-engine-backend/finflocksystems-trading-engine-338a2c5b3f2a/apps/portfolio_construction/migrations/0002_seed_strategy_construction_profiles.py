from django.db import migrations


PROFILES = {
    "RSI_MEAN_REVERSION": {
        "minimum_risk": 2,
        "maximum_risk": 5,
        "summary": "Mean-reversion strategy for manually selected stocks.",
        "limitations": "Long-only construction; strategy signals remain disabled after apply.",
    },
    "SMA_CROSSOVER": {
        "minimum_risk": 2,
        "maximum_risk": 5,
        "summary": "Trend-following crossover strategy for manually selected stocks.",
        "limitations": "Long-only construction; strategy signals remain disabled after apply.",
    },
    "DONCHIAN_BREAKOUT": {
        "minimum_risk": 3,
        "maximum_risk": 5,
        "summary": "Breakout strategy for balanced through aggressive goals.",
        "limitations": "Long-only construction; strategy signals remain disabled after apply.",
    },
    "VOLATILITY_TARGET_MOMENTUM": {
        "minimum_risk": 3,
        "maximum_risk": 5,
        "summary": "Volatility-scaled momentum strategy for longer-horizon risk-taking goals.",
        "limitations": "Long-only parameters are required; no leverage or short exposure is permitted.",
    },
    "FIXED_WEIGHT_REBALANCE": {
        "minimum_risk": 1,
        "maximum_risk": 5,
        "summary": "Simple fixed-weight monitoring strategy for a selected stock.",
        "limitations": "The created instance starts disabled and must be reviewed before activation.",
    },
}


def seed_profiles(apps, schema_editor):
    Definition = apps.get_model("strategies", "StrategyDefinition")
    Profile = apps.get_model("portfolio_construction", "StrategyConstructionProfile")
    for key, values in PROFILES.items():
        definition = Definition.objects.filter(key=key).first()
        if definition:
            Profile.objects.update_or_create(
                strategy_definition=definition,
                defaults={
                    **values,
                    "supported_goal_timeframes": ["HURRY", "FAST", "BUILD", "GROW", "COMPOUND"],
                    "construction_enabled": True,
                    "user_selectable": True,
                },
            )


def remove_profiles(apps, schema_editor):
    Profile = apps.get_model("portfolio_construction", "StrategyConstructionProfile")
    Profile.objects.filter(strategy_definition__key__in=PROFILES).delete()


class Migration(migrations.Migration):
    dependencies = [("portfolio_construction", "0001_initial")]
    operations = [migrations.RunPython(seed_profiles, remove_profiles)]
