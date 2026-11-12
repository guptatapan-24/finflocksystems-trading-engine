from types import SimpleNamespace
from apps.strategies.plugins.base import EvaluationContext


def plugin_context(*,parameters,target_weight="0.05",bar=None,indicators=None,previous_indicators=None,state="FLAT"):
    instance=SimpleNamespace(parameters=parameters,target_configuration={"target_weight":target_weight})
    return EvaluationContext(strategy_instance=instance,strategy_version=SimpleNamespace(version=1),
        instrument=SimpleNamespace(pk=1,symbol="TEST"),bar=bar or {"close":"100","is_final":True},
        indicators=indicators or {},previous_indicators=previous_indicators or {},previous_state=state,state_data={})
