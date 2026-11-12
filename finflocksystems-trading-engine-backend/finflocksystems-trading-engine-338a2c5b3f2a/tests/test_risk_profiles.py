import copy
import json
import logging

import pytest
from django.test import override_settings

from apps.accounts.models import BrokerAccount
from apps.instruments.models import BrokerContract, Instrument
from apps.portfolios.models import TradingPortfolio
from apps.strategies import risk_profiles
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyDefinition, StrategyInstance
from apps.strategies.option_templates import AUTO_OPTION_STRATEGY_KEY, option_parameter_schema
from apps.strategies.plugins import get_plugin
from tests.managed_gateway import bind_gateway_mode

pytestmark = pytest.mark.django_db

API = "/api/v1/strategy-instances/"
# The four values the spec changes against today's screen defaults; nothing else may move.
EXPECTED_DIFF_KEYS = {"rps_threshold", "rps_iv_score_threshold", "rps_tqs_threshold", "one_entry_per_expiry"}
PROFILE_KEYS = ("rps_threshold", "rps_iv_score_threshold", "rps_tqs_threshold")
ON = override_settings(RISK_PROFILING_ENABLED=True)


@pytest.fixture
def portfolio():
    account = BrokerAccount.objects.create(account_id="DU-RISKPROF", net_liquidation=100000, available_cash=100000, buying_power=200000)
    item = TradingPortfolio.objects.create(name="Risk profile paper", account=account, minimum_notional=1)
    bind_gateway_mode(item)
    return item


@pytest.fixture
def underlying():
    item = Instrument.objects.create(symbol="NIFTY50", exchange="NSE", currency="INR", asset_class="IND")
    BrokerContract.objects.create(instrument=item, conid=4242, primary_exchange="NSE", local_symbol="NIFTY50")
    return item


def plugin(key=AUTO_OPTION_STRATEGY_KEY):
    return get_plugin(StrategyDefinition.objects.get(key=key))


def post(client, portfolio, underlying, name, key=AUTO_OPTION_STRATEGY_KEY, **extra):
    payload = {"name": name, "definition_key": key, "instrument_id": underlying.pk, "portfolio_id": portfolio.pk,
               "timeframe": "1d", "target_configuration": {"target_weight": 0, "capital_share": 1, "priority": 100},
               "qualify": False, **extra}
    return client.post(API, payload, content_type="application/json")


def patch(client, instance_id, **body):
    return client.patch(f"{API}{instance_id}/", body, content_type="application/json")


# ---- resolver -------------------------------------------------------------------------------

def test_resolver_gives_base_plus_the_profile_values():
    expected = {"CONSERVATIVE": (41, 30, 39), "NEUTRAL": (38, 25, 37), "AGGRESSIVE": (35, 20, 35)}
    for name, values in expected.items():
        resolved = risk_profiles.resolve_parameters(name)
        assert tuple(resolved[key] for key in PROFILE_KEYS) == values
        assert resolved["one_entry_per_expiry"] is False


def test_only_the_three_profile_parameters_differ_between_profiles():
    resolved = {name: risk_profiles.resolve_parameters(name) for name in risk_profiles.PROFILE_NAMES}
    for left in resolved.values():
        for right in resolved.values():
            assert {key for key in left if left[key] != right[key]} <= set(PROFILE_KEYS)
    assert {key for key in resolved["CONSERVATIVE"] if resolved["CONSERVATIVE"][key] != resolved["AGGRESSIVE"][key]} == set(PROFILE_KEYS)


def test_no_profile_falls_back_to_neutral():
    assert risk_profiles.resolve_parameters(None) == risk_profiles.resolve_parameters("NEUTRAL")
    assert risk_profiles.DEFAULT_PROFILE == "NEUTRAL"


@pytest.mark.parametrize("bad", ["RISKY", "neutral", "", 7, ["NEUTRAL"]])
def test_unknown_profile_name_is_rejected_with_a_clear_error(bad):
    if bad == "":
        assert risk_profiles.normalise_profile(bad) == "NEUTRAL"
        return
    with pytest.raises(ValueError, match="Unknown risk_profile .* CONSERVATIVE, NEUTRAL, AGGRESSIVE"):
        risk_profiles.normalise_profile(bad)


# ---- safe merge: key parity, type parity, golden comparison ------------------------------------

def test_key_parity_with_the_real_parameter_schema_in_both_directions():
    schema_keys = set(option_parameter_schema()["properties"])
    for name in risk_profiles.PROFILE_NAMES:
        assert set(risk_profiles.resolve_parameters(name)) == schema_keys
    assert len(risk_profiles.resolve_profile("NEUTRAL")) == 44  # the spec's 44 parameters
    assert set(risk_profiles.resolve_parameters("NEUTRAL")) == set(plugin().default_parameters)
    assert set(risk_profiles.SPEC_TO_SCHEMA.values()) <= schema_keys


def test_type_parity_through_the_same_validation_path_as_the_form():
    schema = option_parameter_schema()["properties"]
    py_types = {"integer": int, "number": (int, float), "string": str, "boolean": bool}
    for name in risk_profiles.PROFILE_NAMES:
        resolved = risk_profiles.resolve_parameters(name)
        validated = plugin().validate_configuration(resolved, {})
        assert set(validated) == set(resolved)
        for key, value in resolved.items():
            declared = schema[key]["type"]
            if value is None:
                assert "null" in declared, key
            else:
                assert isinstance(value, py_types[declared if isinstance(declared, str) else [t for t in declared if t != "null"][0]]), key
                assert not (isinstance(value, bool) and declared in ("integer", "number")), key
        # blanks the form submits as "" and the spec stores as null
        assert validated["option_trading_class"] == "" and validated["dhan_security_id"] == "" and validated["dhan_segment"] == ""
        assert validated["rps_max_two_sided_breach_probability"] is None
        assert validated["rps_market_open_time"] == "09:15"


def test_golden_only_the_expected_values_differ_from_the_current_screen_defaults():
    definition_defaults = get_plugin(StrategyDefinition.objects.get(key=AUTO_OPTION_STRATEGY_KEY)).default_parameters
    legacy = plugin().validate_configuration(dict(definition_defaults), {})  # what the screen submits today
    expected_values = {"CONSERVATIVE": (41, 30, 39), "NEUTRAL": (38, 25, 37), "AGGRESSIVE": (35, 20, 35)}
    for name, values in expected_values.items():
        new = plugin().validate_configuration(risk_profiles.resolve_parameters(name), {})
        differences = risk_profiles.diff_parameters(legacy, new)
        assert set(differences) == EXPECTED_DIFF_KEYS
        assert differences["one_entry_per_expiry"] == (True, False)
        assert tuple(differences[key][1] for key in PROFILE_KEYS) == values
        assert all(differences[key][0] == 60 for key in PROFILE_KEYS)


def test_fixed_templates_with_their_own_defaults_are_not_eligible():
    assert "DELTA_PUT_CREDIT_SPREAD" not in risk_profiles.ELIGIBLE_DEFINITION_KEYS
    assert "CALENDAR_CALL" not in risk_profiles.ELIGIBLE_DEFINITION_KEYS
    assert AUTO_OPTION_STRATEGY_KEY in risk_profiles.ELIGIBLE_DEFINITION_KEYS
    for key in risk_profiles.ELIGIBLE_DEFINITION_KEYS:  # parity must hold for every eligible definition
        legacy = plugin(key).validate_configuration({}, {})
        assert set(risk_profiles.diff_parameters(legacy, plugin(key).validate_configuration(risk_profiles.resolve_parameters("NEUTRAL"), {}))) == EXPECTED_DIFF_KEYS


# ---- load-time validation ----------------------------------------------------------------------

def presets():
    with risk_profiles.PRESETS_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def broken(mutate):
    data = presets()
    mutate(data)
    with pytest.raises(risk_profiles.RiskProfileConfigError):
        risk_profiles._validate_presets(data)


def test_the_shipped_presets_pass_validation():
    assert risk_profiles._validate_presets(copy.deepcopy(presets()))


@pytest.mark.parametrize("field,value", [
    ("rps_entry_floor", 42), ("rps_entry_floor", "41"), ("rps_entry_floor", True),
    ("rps_iv_score_threshold", 19), ("rps_iv_score_threshold", 31),
    ("rps_tqs_threshold", 34), ("rps_tqs_threshold", 40),
])
def test_a_value_outside_its_band_fails_at_load(field, value):
    broken(lambda data: data["profiles"]["NEUTRAL"].__setitem__(field, value))


def test_band_edges_are_allowed():
    data = presets()
    data["profiles"]["AGGRESSIVE"].update(rps_entry_floor=41, rps_iv_score_threshold=20, rps_tqs_threshold=35)
    data["profiles"]["CONSERVATIVE"].update(rps_iv_score_threshold=30, rps_tqs_threshold=39)
    assert risk_profiles._validate_presets(data)


def test_one_entry_per_expiry_must_be_off():
    broken(lambda data: data["base"].__setitem__("one_entry_per_expiry", True))
    broken(lambda data: data["profiles"]["NEUTRAL"].__setitem__("one_entry_per_expiry", True))


def test_structural_breakage_fails_at_load():
    broken(lambda data: data["profiles"].pop("NEUTRAL"))
    broken(lambda data: data["profiles"].__setitem__("EXTRA", dict(data["profiles"]["NEUTRAL"])))
    broken(lambda data: data["profiles"]["NEUTRAL"].pop("rps_tqs_threshold"))
    broken(lambda data: data["base"].__setitem__("invented_key", 1))
    broken(lambda data: data["base"].pop("lots"))
    broken(lambda data: data["base"].__setitem__("rps_entry_floor", 40))


def test_module_load_fails_fast_on_a_bad_file(tmp_path, monkeypatch):
    data = presets()
    data["profiles"]["AGGRESSIVE"]["rps_entry_floor"] = 60
    bad = tmp_path / "presets.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(risk_profiles, "PRESETS_PATH", bad)
    with pytest.raises(risk_profiles.RiskProfileConfigError, match="AGGRESSIVE: rps_entry_floor"):
        risk_profiles._load()


# ---- read-only endpoint ------------------------------------------------------------------------

def test_catalogue_endpoint_is_read_only_and_reports_the_flag(client):
    data = client.get("/api/v1/strategy-risk-profiles/").json()["data"]
    assert data["enabled"] is False and data["default_profile"] == "NEUTRAL"
    assert data["profiles"]["CONSERVATIVE"]["summary"] == {
        "rps_threshold": 41, "rps_iv_score_threshold": 30, "rps_tqs_threshold": 39, "one_entry_per_expiry": False,
    }
    assert data["profiles"]["NEUTRAL"]["parameters"] == risk_profiles.resolve_parameters("NEUTRAL")
    assert [item["label"] for item in data["summary_fields"]] == [
        "RPS Entry Floor", "RPS IV Score Threshold", "RPS TQS Threshold", "One Entry Per Expiry"]
    assert client.post("/api/v1/strategy-risk-profiles/", {}).status_code == 405
    with ON:
        assert client.get("/api/v1/strategy-risk-profiles/").json()["data"]["enabled"] is True


# ---- save path: server decides, client parameters rejected -----------------------------------

def test_flag_off_the_legacy_flow_is_unchanged_and_risk_profile_is_ignored(client, portfolio, underlying):
    created = post(client, portfolio, underlying, "LEGACY_OFF", risk_profile="AGGRESSIVE")
    assert created.status_code == 201
    stored = StrategyInstance.objects.get(pk=created.json()["data"]["id"])
    assert stored.parameters == plugin().validate_configuration({}, {})
    assert stored.parameters["rps_threshold"] == 60 and stored.parameters["one_entry_per_expiry"] is True
    assert "risk_profile" not in stored.target_configuration


def test_flag_on_create_resolves_on_the_server(client, portfolio, underlying):
    with ON:
        for index, name in enumerate(risk_profiles.PROFILE_NAMES):
            created = post(client, portfolio, underlying, f"PROFILED_{index}", risk_profile=name)
            assert created.status_code == 201, created.json()
            stored = StrategyInstance.objects.get(pk=created.json()["data"]["id"])
            assert stored.parameters == plugin().validate_configuration(risk_profiles.resolve_parameters(name), {})
            assert stored.target_configuration["risk_profile"] == name
            assert stored.target_configuration["capital_share"] == 1  # the rest of the save path is untouched


def test_flag_on_without_a_profile_uses_neutral(client, portfolio, underlying):
    with ON:
        created = post(client, portfolio, underlying, "DEFAULTED")
    stored = StrategyInstance.objects.get(pk=created.json()["data"]["id"])
    assert stored.target_configuration["risk_profile"] == "NEUTRAL" and stored.parameters["rps_threshold"] == 38


def test_flag_on_client_parameters_are_rejected_and_nothing_is_saved(client, portfolio, underlying):
    with ON:
        rejected = post(client, portfolio, underlying, "SMUGGLED", risk_profile="NEUTRAL", parameters={"rps_threshold": 1})
        assert rejected.status_code == 400 and "send only risk_profile" in rejected.json()["error"]["message"]
        unknown = post(client, portfolio, underlying, "UNKNOWN", risk_profile="WILD")
        assert unknown.status_code == 400 and "Unknown risk_profile" in unknown.json()["error"]["message"]
        spoofed = post(client, portfolio, underlying, "SPOOFED", risk_profile="AGGRESSIVE",
                       target_configuration={"capital_share": 1, "priority": 100, "risk_profile": "CONSERVATIVE"})
    assert not StrategyInstance.objects.filter(name__in=["SMUGGLED", "UNKNOWN"]).exists()
    assert StrategyInstance.objects.get(name="SPOOFED").target_configuration["risk_profile"] == "AGGRESSIVE"


def test_flag_on_an_ineligible_definition_keeps_the_legacy_form(client, portfolio, underlying):
    with ON:
        created = post(client, portfolio, underlying, "DELTA_LEGACY", key="DELTA_PUT_CREDIT_SPREAD",
                       parameters={"rps_threshold": 61})
    assert created.status_code == 201
    stored = StrategyInstance.objects.get(pk=created.json()["data"]["id"])
    assert stored.parameters["rps_threshold"] == 61 and "risk_profile" not in stored.target_configuration


def test_patch_on_a_profiled_strategy(client, portfolio, underlying):
    with ON:
        item_id = post(client, portfolio, underlying, "SWITCHING", risk_profile="NEUTRAL").json()["data"]["id"]
        blocked = patch(client, item_id, parameters={"rps_threshold": 1})
        assert blocked.status_code == 400 and "set by the server" in blocked.json()["error"]["message"]
        renamed = patch(client, item_id, name="SWITCHING_RENAMED")
        assert renamed.status_code == 200 and renamed.json()["data"]["version"] == 1
        # a client replacing target_configuration cannot drop the profile
        patch(client, item_id, target_configuration={"capital_share": 0.5, "priority": 90})
        assert StrategyInstance.objects.get(pk=item_id).target_configuration["risk_profile"] == "NEUTRAL"
        switched = patch(client, item_id, risk_profile="CONSERVATIVE")
        assert switched.status_code == 200
        both = patch(client, item_id, risk_profile="CONSERVATIVE", parameters={"rps_threshold": 1})
        assert both.status_code == 400
        bad = patch(client, item_id, risk_profile="WILD")
        assert bad.status_code == 400
    stored = StrategyInstance.objects.get(pk=item_id)
    assert stored.target_configuration["risk_profile"] == "CONSERVATIVE"
    assert stored.target_configuration["capital_share"] == 0.5
    assert stored.parameters == plugin().validate_configuration(risk_profiles.resolve_parameters("CONSERVATIVE"), {})
    assert stored.version == 3  # create, then the two material saves; the rename created none


# ---- existing strategies, rollback, shadow -------------------------------------------------------

def test_existing_strategy_without_a_profile_keeps_its_stored_parameters(client, portfolio, underlying):
    legacy_parameters = {**plugin().validate_configuration({}, {}), "rps_threshold": 55, "rps_iv_score_threshold": 50}
    instance, _ = create_instance(name="OLD", definition_key=AUTO_OPTION_STRATEGY_KEY, portfolio=portfolio, instrument_id=underlying.pk,
                                  timeframe="1d", parameters=legacy_parameters, target_configuration={"target_weight": 0}, qualify=False)
    before = StrategyInstance.objects.get(pk=instance.pk).parameters
    with ON:
        # the activation preflight re-validates stored parameters with the same plugin; it must still pass
        assert get_plugin(instance.definition).validate_configuration(instance.parameters, instance.target_configuration) == before
        assert patch(client, instance.pk, name="OLD_RENAMED").status_code == 200
        # legacy edit still works (the edit panel sends the whole parameter object)
        assert patch(client, instance.pk, parameters={**before, "rps_threshold": 52}).status_code == 200
    after = StrategyInstance.objects.get(pk=instance.pk)
    assert after.parameters["rps_threshold"] == 52 and after.parameters["rps_iv_score_threshold"] == 50
    assert "risk_profile" not in after.target_configuration


def test_rollback_turning_the_flag_off_restores_legacy_behaviour_without_cleanup(client, portfolio, underlying):
    with ON:
        item_id = post(client, portfolio, underlying, "ROLLBACK", risk_profile="AGGRESSIVE").json()["data"]["id"]
    # flag off again: the stored profiled strategy is valid and editable the old way
    assert patch(client, item_id, parameters={"rps_threshold": 50}).status_code == 200
    assert StrategyInstance.objects.get(pk=item_id).parameters["rps_threshold"] == 50
    assert post(client, portfolio, underlying, "LEGACY_AFTER", parameters={"rps_threshold": 61}).status_code == 201


def test_shadow_mode_reports_the_difference_and_changes_nothing(client, portfolio, underlying, caplog):
    with override_settings(RISK_PROFILING_SHADOW=True), caplog.at_level(logging.INFO, logger="apps.strategies.risk_profiles"):
        created = post(client, portfolio, underlying, "SHADOWED")
    assert created.status_code == 201
    stored = StrategyInstance.objects.get(pk=created.json()["data"]["id"])
    assert stored.parameters["rps_threshold"] == 60 and stored.parameters["one_entry_per_expiry"] is True
    line = next(record.getMessage() for record in caplog.records if "risk_profile_shadow" in record.getMessage())
    logged = json.loads(line.split("differences=", 1)[1])
    assert set(logged) == EXPECTED_DIFF_KEYS and logged["rps_threshold"] == [60, 38]


def test_shadow_failure_never_breaks_the_request(monkeypatch):
    monkeypatch.setattr(risk_profiles, "resolve_parameters", lambda profile=None: 1 / 0)
    with override_settings(RISK_PROFILING_SHADOW=True):
        assert risk_profiles.log_shadow(AUTO_OPTION_STRATEGY_KEY, {}) is None
