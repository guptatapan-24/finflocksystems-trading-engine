from datetime import date
from decimal import Decimal

from apps.instruments.models import Instrument

from ..option_templates import (
    AUTO_OPTION_STRATEGY_KEY,
    COMMON_DEFAULTS,
    OPTION_TEMPLATES,
    option_parameter_schema,
)
from .base import D, EvaluationContext, StrategyDecision, StrategyLegTarget, StrategyPlugin, StreamInput


class OptionStrategyPlugin(StrategyPlugin):
    """Runtime adapter for the seeded RPS option templates.

    Entry is decided upstream in `option_execution`; this plugin turns the
    approved selection into leg targets and applies the lifecycle.  Positions
    are held to expiry: the only discretionary close is one of the three
    profit/regime soft exits, which `option_execution.prepare_exit_context`
    evaluates and reports as `SOFT_EXIT`.
    """

    key = "OPTION_TEMPLATE"
    name = "Option strategy"
    strategy_kind = "OPTIONS"
    execution_model = "MULTI_LEG_QUANTITY"
    requires_option_context = True
    supported_asset_types = ("STK", "IND")
    supported_directions = ("MULTI_LEG",)
    # The source's RPS is deliberately timeframe-independent: every derived
    # feature is cut strictly before the decision session and the session is
    # scored against its opening print, so a 1m, 5m or 15m replay produces the
    # same `rps_base` as a daily one (`trading/rps.py`,
    # `build_rps_observations`). The execution timeframe only decides how often
    # the entry window is sampled and at what price a fill happens, so the
    # strategy accepts the same timeframes the source can replay.
    supported_timeframes = ("1m", "5m", "15m", "1h", "1d")
    default_parameters = COMMON_DEFAULTS
    parameter_schema = option_parameter_schema()

    @classmethod
    def for_definition(cls, definition):
        plugin = cls()
        plugin.key = definition.key
        plugin.name = definition.name
        plugin.description = definition.description
        current_schema = option_parameter_schema()
        stored_schema = definition.parameter_schema or {}
        plugin.parameter_schema = {**current_schema, **stored_schema, "properties": {
            **current_schema["properties"], **stored_schema.get("properties", {}),
        }}
        plugin.default_parameters = {
            **COMMON_DEFAULTS,
            **{
                name: specification.get("default")
                for name, specification in plugin.parameter_schema.get("properties", {}).items()
                if "default" in specification
            },
        }
        plugin.supported_timeframes = tuple(definition.supported_timeframes or ("1d",))
        return plugin

    def validate_semantics(self, parameters, target_configuration):
        if parameters["entry_min_dte"] > parameters["entry_max_dte"]:
            raise ValueError("entry_min_dte must not exceed entry_max_dte")
        if D(parameters["atr_multiplier"]) != Decimal("0.75"):
            raise ValueError("The RPS V2 option boundary requires an ATR multiplier of exactly 0.75")

    def required_stream_inputs(self, parameters):
        period = int(parameters["atr_period"])
        warmup = max(75, period * 5 + 5)
        # The RPS features are derived in `option_analytics.rps` from completed
        # daily bars, never from this stream, so the only stream input the
        # decision needs is the bar series that paces evaluation.
        #
        # `weekly_atr` and `weekly_close` used to be declared here. They have no
        # counterpart in the source and already decided nothing, but a declared
        # INDICATOR must carry a non-null value on a warm-up bar before
        # `_warmup_inputs_ready` releases the strategy. Both are null on every
        # intraday bar -- a 14-period weekly figure needs about fourteen weeks
        # of history, which exceeds IBKR's ninety-day intraday cap -- so
        # declaring them made every timeframe below 1d impossible to activate.
        return [StreamInput("BAR", "OHLCV", warmup_bars=warmup)]

    def evaluate(self, context: EvaluationContext):
        option_context = context.bar.get("option_context") or {}
        prior_state = dict(context.state_data or {})
        held = tuple(context.attributed_positions or ())
        if held:
            return self._evaluate_exit(context, held, prior_state, option_context)

        if prior_state.get("exit_requested"):
            if context.active_orders:
                return StrategyDecision(
                    "HOLD", None, "FLAT", "Option exit orders are still active",
                    "EXIT_PENDING", state_data=prior_state,
                    targets=self._targets_from_state(prior_state, exit=True),
                )
            entered = prior_state.get("entered_expiries", [])
            prior_state = {"entered_expiries": entered}

        if context.active_orders and prior_state.get("legs"):
            return StrategyDecision(
                "HOLD", None, "MULTI_LEG", "Option entry orders are still active",
                "ENTRY_PENDING", state_data=prior_state,
                targets=self._targets_from_state(prior_state),
            )

        status = option_context.get("status")
        entry_check = self._entry_check(context, option_context)
        if status != "READY":
            next_data = {
                **prior_state, **(option_context.get("state_data") or {}),
                "last_entry_check": entry_check,
            }
            return StrategyDecision(
                "NO_ACTION", None, "FLAT",
                option_context.get("reason") or "RPS option entry context is not ready",
                "FLAT", state_data=next_data,
            )

        selected_key = option_context["selected_strategy"]
        if self.key != AUTO_OPTION_STRATEGY_KEY and selected_key != self.key:
            raise ValueError(f"Option selection returned {selected_key} for fixed strategy {self.key}")
        entered_expiries = set(prior_state.get("entered_expiries") or [])
        selected_expiries = {str(item["expiration"]) for item in option_context["legs"]}
        if self._boolean(context.strategy_instance.parameters.get("one_entry_per_expiry", True)) and entered_expiries & selected_expiries:
            reason = "The strategy already entered this contract expiry"
            return StrategyDecision(
                "NO_ACTION", None, "FLAT", reason,
                "FLAT", state_data={
                    **prior_state,
                    "last_entry_check": {**entry_check, "status": "EXPIRY_ALREADY_ENTERED", "reason": reason},
                },
            )

        lots = D(context.strategy_instance.parameters.get("lots", 1))
        legs = []
        state_legs = []
        for leg in option_context["legs"]:
            instrument = Instrument.objects.select_related("option_contract").get(pk=leg["instrument_id"])
            quantity = lots * D(instrument.lot_size) * D(leg.get("ratio", 1)) * (Decimal(1) if leg["side"] == "BUY" else Decimal(-1))
            legs.append(StrategyLegTarget(
                instrument=instrument,
                quantity=quantity,
                direction="LONG" if quantity > 0 else "SHORT",
                role=leg["role"],
                metadata={
                    "option_strategy": selected_key,
                    "expiration": leg["expiration"],
                    "side": leg["side"],
                    "hedge_first": leg["side"] == "BUY",
                    "lifecycle_locked": True,
                    "dhan_oi": leg.get("dhan_oi"),
                    "dhan_volume": leg.get("dhan_volume"),
                    "dhan_price": leg.get("dhan_price"),
                },
            ))
            state_legs.append({**leg, "target_quantity": str(quantity)})
        opened_at = str(context.bar.get("window_end") or context.event_metadata.get("event_time") or "")[:10]
        next_data = {
            **prior_state,
            "selected_strategy": selected_key,
            "opened_at": opened_at,
            "expiration": min(selected_expiries),
            "legs": state_legs,
            "entry_score": option_context.get("entry_score"),
            "selection_snapshot_id": option_context.get("selection_snapshot_id"),
            # The range, volume and IV the three soft exits are measured
            # against, fixed at entry and carried for the position's life.
            "exit_baseline": option_context.get("exit_baseline"),
            "entered_expiries": sorted(entered_expiries | selected_expiries),
            "exit_requested": False,
            "last_entry_check": entry_check,
        }
        return StrategyDecision(
            "ENTER_OPTIONS", None, "MULTI_LEG", option_context["reason"],
            "ENTRY_PENDING", confidence=D(option_context.get("entry_score", 0)) / Decimal(100),
            state_data=next_data, targets=tuple(legs),
        )

    def _evaluate_exit(self, context, held, state_data, option_context):
        expiries = [
            item.instrument.option_contract.expiration
            for item in held
            if hasattr(item.instrument, "option_contract")
        ]
        if not expiries:
            raise ValueError("Attributed option strategy position has no option expiry")
        event_date = date.fromisoformat(str(context.bar.get("window_end"))[:10])
        opened = date.fromisoformat(state_data["opened_at"]) if state_data.get("opened_at") else event_date
        hold_days = int(context.strategy_instance.parameters.get("hold_days", 0))
        exit_dte = int(context.strategy_instance.parameters.get("exit_dte", 0))
        nearest_expiry = min(expiries)
        expiry_exit = (nearest_expiry - event_date).days <= exit_dte
        strategy_exit = hold_days > 0 and (event_date - opened).days >= hold_days
        decision = option_context.get("exit_decision") or {}
        soft_exit = option_context.get("status") == "SOFT_EXIT"
        # The running peak P&L the giveback rules measure against moves every
        # bar, so the refreshed baseline is kept whether or not anything fired.
        if option_context.get("exit_baseline"):
            state_data = {**state_data, "exit_baseline": option_context["exit_baseline"]}
        if not (expiry_exit or strategy_exit or soft_exit):
            if context.active_orders and state_data.get("legs"):
                targets = self._targets_from_state(state_data)
            else:
                targets = tuple(
                    StrategyLegTarget(
                        instrument=item.instrument,
                        quantity=D(item.quantity),
                        direction="LONG" if D(item.quantity) > 0 else "SHORT",
                        role=self._state_leg(state_data, item.instrument_id).get("role", ""),
                        metadata=self._target_metadata(
                            state_data,
                            self._state_leg(state_data, item.instrument_id),
                        ),
                    )
                    for item in held
                )
            return StrategyDecision(
                "HOLD_OPTIONS", None, "MULTI_LEG", "Selected option strategy remains inside its lifecycle",
                "LONG", state_data=state_data, targets=targets,
            )
        if expiry_exit:
            reason = "Contract expiry exit"
        elif strategy_exit:
            reason = "Strategy hold-period exit"
        else:
            reason = "Approved soft exit: " + "; ".join(decision.get("reason_codes") or [])
        targets = tuple(
            StrategyLegTarget(
                instrument=item.instrument,
                quantity=Decimal(0),
                direction="FLAT",
                role=next((leg.get("role", "") for leg in state_data.get("legs", []) if leg.get("instrument_id") == item.instrument_id), ""),
                metadata={
                    "option_strategy": state_data.get("selected_strategy", self.key),
                    "expiration": item.instrument.option_contract.expiration.isoformat(),
                    "lifecycle_locked": True,
                    "exit": True,
                },
            )
            for item in held
        )
        return StrategyDecision(
            "EXIT_OPTIONS", None, "FLAT", reason, "EXIT_PENDING",
            state_data={
                **state_data, "exit_requested": True, "exit_reason": reason,
                "exit_reason_codes": decision.get("reason_codes") if soft_exit else None,
                "exit_metrics": decision.get("metrics") if soft_exit else None,
            },
            targets=targets,
        )

    @staticmethod
    def _entry_check(context, option_context):
        """The latest entry decision, whole and self-contained.

        Refusals are the normal outcome, so the operator question is always
        which gate refused and on what number. This record answers it for the
        bar just evaluated alone: nothing is carried over from an earlier bar,
        so an early refusal (entry window, no expiry) never shows the evidence
        of a later gate from a previous session as if it were current.
        """
        parameters = context.strategy_instance.parameters
        observation = option_context.get("rps_observation") or {}
        return {
            "status": option_context.get("status") or "UNKNOWN",
            "reason": option_context.get("reason") or "RPS option entry context is not ready",
            "evaluated_at": str(context.bar.get("window_end") or context.event_metadata.get("event_time") or ""),
            "entry_score": option_context.get("entry_score"),
            "entry_threshold": option_context.get("entry_threshold", str(parameters.get("rps_threshold"))),
            "iv_score_threshold": str(parameters.get("rps_iv_score_threshold", 60)),
            "entry_score_components": observation.get("component_scores"),
            "spot": observation.get("spot"),
            "atr_support": observation.get("atr_support"),
            "atr_resistance": observation.get("atr_resistance"),
            "oi_support": observation.get("oi_support"),
            "oi_resistance": observation.get("oi_resistance"),
            "iv_rank_252": observation.get("iv_rank_252"),
            "iv_percentile_252": observation.get("iv_percentile_252"),
            "chain_features_available": observation.get("chain_features_available"),
            "selection_evidence": option_context.get("selection_evidence"),
        }

    def _targets_from_state(self, state_data, *, exit=False):
        from apps.instruments.models import Instrument

        legs = list(state_data.get("legs") or [])
        instruments = {
            item.pk: item
            for item in Instrument.objects.filter(
                pk__in=[int(leg["instrument_id"]) for leg in legs]
            ).select_related("option_contract")
        }
        return tuple(
            StrategyLegTarget(
                instrument=instruments[int(leg["instrument_id"])],
                quantity=Decimal(0) if exit else D(leg["target_quantity"]),
                direction=(
                    "FLAT" if exit else
                    ("LONG" if D(leg["target_quantity"]) > 0 else "SHORT")
                ),
                role=leg.get("role", ""),
                metadata=self._target_metadata(state_data, leg, exit=exit),
            )
            for leg in legs
            if int(leg["instrument_id"]) in instruments
        )

    @staticmethod
    def _state_leg(state_data, instrument_id):
        return next(
            (
                leg for leg in state_data.get("legs", [])
                if int(leg.get("instrument_id", 0)) == int(instrument_id)
            ),
            {},
        )

    def _target_metadata(self, state_data, leg, *, exit=False):
        return {
            "option_strategy": state_data.get("selected_strategy", self.key),
            "expiration": leg.get("expiration") or state_data.get("expiration"),
            "side": leg.get("side"),
            "hedge_first": leg.get("side") == "BUY",
            "lifecycle_locked": True,
            "dhan_oi": leg.get("dhan_oi"),
            "dhan_volume": leg.get("dhan_volume"),
            "dhan_price": leg.get("dhan_price"),
            "exit": exit,
        }

    @staticmethod
    def _boolean(value):
        return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
