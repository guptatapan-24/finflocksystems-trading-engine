from decimal import Decimal
import pytest
from apps.position_sizing.services import calculate_limits, volatility_scaled_candidates


@pytest.mark.parametrize("field,kwargs",[
    ("RISK",{"stop_price":"99","max_loss_fraction":"0.001"}),
    ("WEIGHT",{"max_weight":"0.01"}),
    ("LIQUIDITY",{"adv":"100","participation":"0.01"}),
    ("CASH",{"available_cash":"50"}),
    ("BROKER",{"broker_max_quantity":"2"}),
])
def test_each_quantity_limit_can_bind(field,kwargs):
    base=dict(target_quantity=100,entry_price=10,stop_price=None,nav=10000,available_cash=10000,adv=100000,broker_max_quantity=100)
    base.update(kwargs); assert calculate_limits(**base)["binding"]==field


def test_invalid_stop_and_short_inventory_reject():
    invalid=calculate_limits(target_quantity=10,entry_price=100,stop_price=100,nav=10000,available_cash=10000,adv=10000)
    assert invalid["approved"]==0 and invalid["rejected_reason"]
    short=calculate_limits(target_quantity=10,entry_price=100,stop_price=None,nav=10000,available_cash=10000,adv=10000,side="SELL",short_available=False)
    assert short["approved"]==0 and short["binding"]=="BROKER"


def test_volatility_weights_are_capped_and_renormalized():
    weights=volatility_scaled_candidates({"A":{"score":2,"volatility":1},"B":{"score":1,"volatility":1},"C":{"score":1,"volatility":1}},"0.4")
    assert max(abs(x) for x in weights.values())<=Decimal("0.4")
    assert sum(abs(x) for x in weights.values())==pytest.approx(Decimal("1"))
