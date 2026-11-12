from importlib import import_module
from .builtin import (DonchianBreakoutPlugin, FixedWeightPlugin, RSIMeanReversionPlugin,
    SMACrossoverPlugin, VolatilityTargetMomentumPlugin)

BUILTINS = {plugin.key: plugin for plugin in [RSIMeanReversionPlugin(), SMACrossoverPlugin(),
    DonchianBreakoutPlugin(), VolatilityTargetMomentumPlugin(), FixedWeightPlugin()]}


def get_plugin(definition_or_key):
    key = definition_or_key if isinstance(definition_or_key, str) else definition_or_key.key
    if key in BUILTINS:
        return BUILTINS[key]
    if isinstance(definition_or_key, str):
        raise ValueError(f"Unknown strategy plugin: {key}")
    module_name, class_name = definition_or_key.plugin_path.rsplit(".", 1)
    plugin_class = getattr(import_module(module_name), class_name)
    plugin = plugin_class.for_definition(definition_or_key) if hasattr(plugin_class, "for_definition") else plugin_class()
    if plugin.key != key:
        raise ValueError(f"Plugin key mismatch: expected {key}, got {plugin.key}")
    return plugin


def plugin_catalog():
    return list(BUILTINS.values())
