from .option_templates import (
    AUTO_OPTION_STRATEGY_KEY,
    OPTION_TEMPLATES,
    option_parameter_schema,
)


def seed_option_strategy_definitions(Definition):
    plugin_path = "apps.strategies.plugins.options.OptionStrategyPlugin"
    common = {
        "plugin_path": plugin_path,
        "input_requirements": [],
        "supported_asset_types": ["STK", "IND"],
        "supported_directions": ["MULTI_LEG"],
        # The source replays 1m, 5m and 15m as readily as daily; RPS is scored
        # per session and is timeframe-independent by construction.
        "supported_timeframes": ["1m", "5m", "15m", "1h", "1d"],
        "enabled": True,
    }
    for key, template in OPTION_TEMPLATES.items():
        schema = option_parameter_schema()
        for name, value in template.get("parameter_defaults", {}).items():
            schema["properties"][name]["default"] = value
        Definition.objects.update_or_create(
            key=key,
            defaults={
                **common,
                "parameter_schema": schema,
                "name": template["name"],
                "description": template["description"],
            },
        )
    Definition.objects.update_or_create(
        key=AUTO_OPTION_STRATEGY_KEY,
        defaults={
            **common,
            "parameter_schema": option_parameter_schema(),
            "name": "RPS Options Auto Selector",
            "description": (
                "Uses the frozen prior-close ± 0.75 × weekly Wilder ATR(14) boundary "
                "and top-five put/call OI walls to gate entry, then locks the "
                "minimum-risk supported option structure until its strategy or expiry exit."
            ),
        },
    )


def seed_all_strategy_definitions(apps, schema_editor=None):
    Definition = apps.get_model("strategies", "StrategyDefinition")
    seed_option_strategy_definitions(Definition)
