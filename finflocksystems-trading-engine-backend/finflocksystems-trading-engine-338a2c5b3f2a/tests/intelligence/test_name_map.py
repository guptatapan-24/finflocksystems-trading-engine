import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_intel_names.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_intel_names", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_mapped_engine_name_still_exists():
    module = _load()
    assert module.verify() == []


def test_map_covers_the_models_the_design_depends_on():
    module = _load()
    mapped = {(app, model) for _, app, model, _ in module.MODEL_MAP}
    for required in [("execution", "Fill"), ("oms", "Order"), ("strategies", "StrategyInstance"),
                     ("strategies", "StrategyVersion"), ("strategies", "OptionSelectionSnapshot"),
                     ("allocation", "OrderIntentAttribution"), ("audit", "AuditEvent")]:
        assert required in mapped
