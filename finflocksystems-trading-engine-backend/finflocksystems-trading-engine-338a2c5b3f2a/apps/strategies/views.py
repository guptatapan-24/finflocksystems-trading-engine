import json
import hashlib
import uuid
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from apps.core.views import method_guard, response
from apps.core.idempotency import IdempotencyConflict, canonical_request_hash, require_matching_request
from apps.broker_gateway.client import (
    GatewayCommandRejected,
    GatewayCommandTimeout,
    GatewayError,
    GatewayRouteError,
    GatewaySessionUnavailable,
)
from apps.broker_gateway.models import BrokerGatewaySession
from apps.instruments.models import Instrument
from apps.instruments.services import (
    option_chain,
    qualify_option_contract,
    resolve_instrument,
    search_broker_instruments,
)
from apps.portfolios.models import TradingPortfolio
from .deletion import (
    StrategyDeletionError,
    audit_strategy_deletion_rejection,
    delete_strategy_instance,
)
from .framework import (
    StrategyActivationError,
    create_instance,
    enable_instance,
    flatten_instance,
    pause_instance,
    update_instance,
)
from .models import (OrderPolicy, StrategyAction, StrategyAttributedPosition, StrategyDefinition,
    StrategyInputBinding, StrategyInstance, StrategyRiskPolicy, StrategyRun)
from .models import StrategyVersion
from .option_analytics import rps as rps_engine
from .option_templates import OPTION_TEMPLATES, AUTO_OPTION_STRATEGY_KEY
from .plugins import get_plugin
from .workflow import execution_workflow


class BrokerSessionRequired(ValueError):
    pass


def _definition(item):
    plugin=get_plugin(item)
    return {"id":item.pk,"key":item.key,"name":item.name,"description":item.description,"plugin_path":item.plugin_path,
        "input_requirements":[{"input_type":x.input_type,"name":x.name,"parameters":x.parameters,"warmup_bars":x.warmup_bars}
            for x in plugin.required_stream_inputs(plugin.default_parameters)],
        "parameter_schema":plugin.parameter_schema,"default_parameters":plugin.default_parameters,
        "strategy_kind":plugin.strategy_kind,"execution_model":plugin.execution_model,
        "supported_asset_types":item.supported_asset_types,"supported_directions":item.supported_directions,
        "supported_timeframes":item.supported_timeframes,"version":item.version,"enabled":item.enabled}


def definitions(request, key=None):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    if key:
        try:return response(_definition(StrategyDefinition.objects.get(key=key.upper())))
        except StrategyDefinition.DoesNotExist:return response(status=404,error={"code":"NOT_FOUND","message":"Strategy definition not found","details":{}})
    return response([_definition(x) for x in StrategyDefinition.objects.filter(enabled=True).order_by("name")])


def _risk_profiles():
    """The risk-profile module, only while one of its flags is on, so the legacy path never loads it."""
    if getattr(settings,"RISK_PROFILING_ENABLED",False) or getattr(settings,"RISK_PROFILING_SHADOW",False):
        from . import risk_profiles
        return risk_profiles
    return None


def risk_profile_catalogue(request):
    """Read-only presets and their resolved values; the frontend never duplicates them."""
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from . import risk_profiles as profiles
    return response(profiles.catalogue())


def _strategy_queryset(detail=False):
    from apps.allocation.models import OrderIntentAttribution
    from apps.execution.models import Fill
    from apps.market_streams.health import annotate_stream_health
    from apps.oms.models import Order, OrderIntent
    from .models import StrategySignal, StrategyTarget
    latest_signal=StrategySignal.objects.filter(strategy_instance_id=OuterRef("pk")).order_by("-signal_time","-id")
    latest_target=StrategyTarget.objects.filter(strategy_instance_id=OuterRef("pk"),status="ACTIVE").order_by("-created_at","-id")
    any_target=StrategyTarget.objects.filter(
        strategy_instance_id=OuterRef("pk")
    ).order_by("-created_at","-id")
    attributed=StrategyAttributedPosition.objects.filter(strategy_instance_id=OuterRef("pk"),
        instrument_id=OuterRef("instrument_id"),portfolio_id=OuterRef("portfolio_id"))
    active_attribution=OrderIntentAttribution.objects.filter(strategy_instance_id=OuterRef("pk"),
        order_intent__order__status__in=["CREATED","RISK_APPROVED","QUEUED","SUBMITTED","ACKNOWLEDGED","PARTIALLY_FILLED"]
        ).order_by("-order_intent__order__created_at")
    last_fill=Fill.objects.filter(order__intent__attributions__strategy_instance_id=OuterRef("pk")).order_by("-executed_at","-id")
    latest_order=Order.objects.filter(
        intent__attributions__strategy_instance_id=OuterRef("pk")
    ).order_by("-created_at","-id")
    latest_intent=OrderIntent.objects.filter(
        attributions__strategy_instance_id=OuterRef("pk")
    ).order_by("-created_at","-id")
    latest_action=StrategyAction.objects.filter(
        strategy_instance_id=OuterRef("pk"),action="enable"
    ).order_by("-created_at","-pk")
    query=StrategyInstance.objects.select_related(
        "definition","portfolio__gateway_session","portfolio__account",
        "instrument__broker_contract","instrument__market_state",
        "risk_policy","order_policy"
    ).annotate(
        _latest_signal=Subquery(latest_signal.values("signal_type")[:1]),
        _current_target=Subquery(latest_target.values("target_weight")[:1]),
        _latest_target_id=Subquery(any_target.values("pk")[:1]),
        _attributed_quantity=Subquery(attributed.values("quantity")[:1]),
        _active_order=Subquery(active_attribution.values("order_intent__order__internal_id")[:1]),
        _last_fill=Subquery(last_fill.values("execution_id")[:1]),
        _latest_order_status=Subquery(latest_order.values("status")[:1]),
        _latest_intent_id=Subquery(latest_intent.values("pk")[:1]),
        _latest_intent_operation_status=Subquery(
            latest_intent.values("operation_status")[:1]
        ),
        _latest_rebalance_id=Subquery(latest_intent.values("rebalance_id")[:1]),
        _activation_action_id=Subquery(latest_action.values("pk")[:1]),
        _activation_action_status=Subquery(latest_action.values("status")[:1]),
        _activation_action_retryable=Subquery(latest_action.values("retryable")[:1]),
        _activation_action_error=Subquery(latest_action.values("last_error")[:1]),
        _activation_action_attempt_count=Subquery(latest_action.values("attempt_count")[:1]),
        _activation_action_idempotency_key=Subquery(latest_action.values("idempotency_key")[:1]),
        _activation_action_created_at=Subquery(latest_action.values("created_at")[:1]),
        _activation_action_completed_at=Subquery(latest_action.values("completed_at")[:1]),
    )
    if detail:
        query=query.prefetch_related(
            Prefetch("versions",queryset=StrategyVersion.objects.order_by("-version")),
            Prefetch("input_bindings",queryset=StrategyInputBinding.objects.select_related("requirement","strategy_version")),
        )
    return annotate_stream_health(query)


def _annotated_activation_operation(item):
    if item._activation_action_id is None:
        return None
    return {
        "id":item._activation_action_id,
        "status":item._activation_action_status,
        "retryable":bool(item._activation_action_retryable),
        "message":item._activation_action_error or "",
        "attempt_count":item._activation_action_attempt_count,
        "idempotency_key":item._activation_action_idempotency_key,
        "created_at":item._activation_action_created_at,
        "completed_at":item._activation_action_completed_at,
    }


def _annotated_workflow_summary(item):
    action_status=item._activation_action_status
    order_status=item._latest_order_status
    order_terminal=order_status in {"FILLED","REJECTED","CANCELLED","EXPIRED"}
    terminal=bool(
        (item.state in {"DISABLED","PAUSED","KILLED"} and not action_status)
        or action_status=="FAILED"
        or order_terminal
        or item.state=="ERROR"
    )
    active=bool(
        not terminal and (
            action_status=="PROCESSING"
            or item.state in {
                "ACTIVATING","SUBSCRIBING","WARMING_UP",
                "READY_WAITING_FOR_LIVE_BAR",
            }
            or item._latest_intent_operation_status in {"PENDING","CLAIMED"}
            or order_status in {
                "CREATED","RISK_APPROVED","QUEUED","BROKER_BLOCKED",
                "SUBMITTED","ACKNOWLEDGED","PARTIALLY_FILLED",
                "CANCEL_PENDING","UNKNOWN",
            }
            or (item._latest_target_id and not item._latest_intent_id)
        )
    )
    if not action_status:
        current_stage="CREATED"
    elif action_status=="FAILED":
        current_stage={
            "ACTIVATING":"CONTRACT_QUALIFIED",
            "SUBSCRIBING":"SUBSCRIPTION_ACTIVE",
            "WARMING_UP":"WARMUP_IN_PROGRESS",
            "READY_WAITING_FOR_LIVE_BAR":"WAITING_FOR_LIVE_BAR",
        }.get(item.state,"ACTIVATION_QUEUED")
    elif not getattr(item.instrument,"broker_contract",None):
        current_stage="CONTRACT_QUALIFIED"
    elif not item.subscription_ready_at:
        current_stage="SUBSCRIPTION_ACTIVE"
    elif not item.warmup_completed_at:
        current_stage="WARMUP_IN_PROGRESS"
    elif not item.first_evaluation_completed_at:
        current_stage="WAITING_FOR_LIVE_BAR"
    elif not item._latest_target_id:
        current_stage="TARGET_GENERATED"
    elif not item._latest_rebalance_id:
        current_stage="REBALANCE_CREATED"
    elif not item._latest_intent_id:
        current_stage="ORDER_INTENT_CREATED"
    elif not order_status or order_status=="CREATED":
        current_stage="RISK_DECISION"
    elif order_status in {"RISK_APPROVED","QUEUED","BROKER_BLOCKED"}:
        current_stage="BROKER_COMMAND_SENT"
    elif order_status=="SUBMITTED":
        current_stage="ORDER_ACKNOWLEDGED"
    else:
        current_stage="FILL_PROGRESS"
    if action_status=="FAILED" or item.state=="ERROR" or order_status=="REJECTED":
        status="FAILED"
    elif order_status=="FILLED":
        status="COMPLETED"
    elif item.state=="DISABLED" and not action_status:
        status="DISABLED"
    elif active:
        status="ACTIVE"
    else:
        status="IDLE"
    return {
        "trace_id":str(item.workflow_trace_id),
        "status":status,
        "active":active,
        "terminal":terminal,
        "current_stage":current_stage,
        "poll_after_ms":0 if terminal else (1000 if active else 12000),
        "observed_at":timezone.now(),
    }


def _instance(item, detail=False):
    from apps.market_streams.models import IndicatorValue
    contract=getattr(item.instrument,"broker_contract",None)
    plugin=get_plugin(item.definition)
    annotated=hasattr(item,"_current_target")
    latest_bar_at=getattr(item,"_stream_last_final_bar",None)
    if not getattr(item,"_stream_annotated",False):
        from apps.market_streams.models import MarketBar
        latest_bar=MarketBar.objects.filter(instrument=item.instrument,interval=item.timeframe,is_final=True).order_by("-window_end","-version").first()
        latest_bar_at=latest_bar.window_end if latest_bar else None
    else:
        latest_bar=None
    market_state=(
        getattr(item.instrument,"market_state",None)
        if detail else None
    )
    price_timestamp=market_state.latest_event_at if market_state else (
        latest_bar.window_end if latest_bar else latest_bar_at
    )
    price_provider=(market_state.reference_price_provider if market_state else "") or (
        latest_bar.provider if latest_bar else getattr(
            item,"_stream_last_final_bar_provider",""
        )
    )
    price_source=(market_state.reference_price_source if market_state else "") or (
        latest_bar.source if latest_bar else getattr(
            item,"_stream_last_final_bar_source",""
        )
    )
    price_value=market_state.reference_price if market_state and market_state.reference_price is not None else (
        latest_bar.close if latest_bar else getattr(
            item,"_stream_last_final_bar_close",None
        )
    )
    price_processing_mode=(
        latest_bar.processing_mode if latest_bar else
        getattr(item,"_stream_last_final_bar_processing_mode",None)
    )
    price_data_kind=(
        "DELAYED"
        if market_state and market_state.market_data_type=="DELAYED"
        else "WARM_UP"
        if price_processing_mode=="WARMUP"
        and not (market_state and market_state.is_execution_usable(execution_mode=item.execution_mode))
        else "LIVE"
    )
    price_fresh=bool(market_state and market_state.is_execution_usable(execution_mode=item.execution_mode))
    price_age=(
        max(0,(timezone.now()-price_timestamp).total_seconds())
        if price_timestamp else None
    )
    latest_signal=getattr(item,"_latest_signal",None) if annotated else item.signals.order_by("-signal_time").values_list("signal_type",flat=True).first()
    latest_target=getattr(item,"_current_target",None) if annotated else item.targets.order_by("-created_at").values_list("target_weight",flat=True).first()
    attributed=getattr(item,"_attributed_quantity",None) if annotated else item.attributed_positions.filter(
        instrument=item.instrument,portfolio=item.portfolio).values_list("quantity",flat=True).first()
    active_order=getattr(item,"_active_order",None) if annotated else item.orderintentattribution_set.filter(
        order_intent__order__status__in=["CREATED","RISK_APPROVED","QUEUED","SUBMITTED","ACKNOWLEDGED","PARTIALLY_FILLED"]
        ).values_list("order_intent__order__internal_id",flat=True).first()
    last_fill=getattr(item,"_last_fill",None)
    latest_indicators={}
    from apps.market_streams.health import strategy_stream_status
    bindings=[binding for binding in item.input_bindings.all()
        if binding.strategy_version.version==item.version and binding.requirement.input_type=="INDICATOR"] if detail else []
    if bindings:
        values={}
        for value in IndicatorValue.objects.filter(instrument=item.instrument,timeframe=item.timeframe,
                requirement_identity_hash__in=[binding.requirement.identity_hash for binding in bindings],is_final=True
                ).order_by("requirement_identity_hash","-event_time","-id"):
            values.setdefault(value.requirement_identity_hash,value)
        from apps.strategies.input_identity import indicator_output_name
        latest_indicators={indicator_output_name(binding.requirement.name,binding.requirement.role):
            values[binding.requirement.identity_hash].value
            for binding in bindings if binding.requirement.identity_hash in values}
    activation_status=(
        item.state if item.state in {
            "DISABLED","ACTIVATING","SUBSCRIBING","WARMING_UP",
            "READY_WAITING_FOR_LIVE_BAR","BLOCKED",
        } else ("ACTIVE" if item.enabled else "DISABLED")
    )
    row={"id":item.pk,"name":item.name,"definition_key":item.definition.key,"definition_name":item.definition.name,
        "portfolio_id":item.portfolio_id,"portfolio":item.portfolio.name,
        "gateway_session_id":str(item.portfolio.gateway_session_id) if item.portfolio.gateway_session_id else None,
        "gateway_session_name":(
            item.portfolio.gateway_session.display_name
            if item.portfolio.gateway_session_id else None
        ),
        "instrument_id":item.instrument_id,
        "symbol":item.instrument.symbol,"asset_class":item.instrument.asset_class,"exchange":item.instrument.exchange,
        "currency":item.instrument.currency,"conid":contract.conid if contract else None,
        "primary_exchange":contract.primary_exchange if contract else None,"timeframe":item.timeframe,
        "parameters":item.parameters,"target_configuration":item.target_configuration,"risk_policy_id":item.risk_policy_id,
        "order_policy_id":item.order_policy_id,"execution_mode":item.execution_mode,"state":item.state,
        "enabled":item.enabled,"activation_status":activation_status,
        "version":item.version,"warmup_progress":item.warmup_progress,
        "warmup_required":max(
            plugin.warmup_bars(item.parameters),
            (
                int(settings.EXECUTION_AVERAGE_VOLUME_WINDOW)
                if settings.EXECUTION_REGISTER_ADV_INPUT else 0
            ),
        ),
        "warmup_started_at":item.warmup_started_at,"warmup_last_progress_at":item.warmup_last_progress_at,
        "subscription_ready_at":item.subscription_ready_at,
        "warmup_completed_at":item.warmup_completed_at,
        "ready_waiting_since":item.ready_waiting_since,
        "first_evaluation_completed_at":item.first_evaluation_completed_at,
        "execution_active_at":item.execution_active_at,
        "activation_stages":{
            "subscription_ready":item.subscription_ready_at is not None,
            "warmup_complete":item.warmup_completed_at is not None,
            "waiting_for_live_bar":item.state=="READY_WAITING_FOR_LIVE_BAR",
            "first_evaluation_complete":item.first_evaluation_completed_at is not None,
            "execution_active":item.execution_active_at is not None,
        },
        "block_reason":item.block_reason,"effective_from":item.effective_from,"effective_to":item.effective_to,
        "last_final_bar":latest_bar_at,"latest_indicators":latest_indicators,
        "latest_signal":latest_signal,"current_target":latest_target,
        "attributed_quantity":attributed or 0,
        "active_order":active_order,
        "last_fill":last_fill,"cooldown":item.state_data.get("cooldown_until"),
        "streaming":strategy_stream_status(item),"created_at":item.created_at,"updated_at":item.updated_at}
    if detail:
        row["activation_operation"]=_annotated_activation_operation(item)
        row["execution_workflow"]=_annotated_workflow_summary(item)
        row["current_price"]={
            "value":price_value,
            "provider":price_provider or "UNKNOWN",
            "source":price_source or "UNKNOWN",
            "data_kind":price_data_kind,
            "market_data_type":market_state.market_data_type if market_state else "LIVE",
            "market_event_at":market_state.market_event_at if market_state else None,
            "timestamp":price_timestamp,
            "age_seconds":round(price_age,3) if price_age is not None else None,
            "stale_after_seconds":market_state.stale_after_seconds if market_state else None,
            "fresh_for_execution":price_fresh,
        }
        row["versions"]=[{"id":x.pk,"version":x.version,"parameter_hash":x.parameter_hash,"configuration_snapshot":x.configuration_snapshot,
            "created_at":x.created_at,"activated_at":x.activated_at,"retired_at":x.retired_at} for x in item.versions.all()]
        row["requirements"]=[{"identity_hash":b.requirement.identity_hash,"input_type":b.requirement.input_type,
            "name":b.requirement.name,"role":b.requirement.role,"parameters":b.requirement.parameters,
            "implementation_version":b.requirement.implementation_version,
            "warmup_bars":b.requirement.warmup_bars,"shared_by":b.requirement.active_ref_count,"active":b.active}
            for b in item.input_bindings.all() if b.strategy_version.version==item.version]
        readiness=(
            item.warmup_readiness_records.select_related(
                "strategy_version"
            ).order_by("-completed_at")[:10]
            if item.warmup_completed_at else []
        )
        row["warmup_readiness"]=[{
            "id":evidence.pk,
            "strategy_version":evidence.strategy_version.version,
            "provider":evidence.provider,
            "provider_generation":evidence.provider_generation,
            "requirement_hashes":evidence.requirement_hashes,
            "requirement_snapshot_hash":evidence.requirement_snapshot_hash,
            "bar_ids":evidence.bar_ids,
            "bar_timestamps":evidence.bar_timestamps,
            "evidence_hash":evidence.evidence_hash,
            "is_current":evidence.is_current,
            "completed_at":evidence.completed_at,
        } for evidence in readiness]
        if plugin.strategy_kind=="OPTIONS":
            selection=item.option_selections.order_by("-observed_at","-pk").first()
            quote_job=item.evaluation_jobs.filter(
                status="WAITING_FOR_INPUT",
                error_details__input_kind="OPTION_QUOTES",
            ).order_by("-updated_at","-pk").first()
            positions=list(item.attributed_positions.exclude(quantity=0).select_related(
                "instrument__broker_contract","instrument__option_contract",
            ))
            evidence_legs = (
                selection.selected_legs if selection else item.state_data.get("legs", [])
            )
            evidence_by_instrument = {
                int(leg["instrument_id"]): leg
                for leg in evidence_legs
                if leg.get("instrument_id") is not None
            }
            row["attributed_quantity"]=sum((abs(position.quantity) for position in positions),0)
            entry_check=item.state_data.get("last_entry_check") or None
            # A selection snapshot is the evidence for the structure that was
            # entered; while flat, the newest refusal is what explains why
            # nothing is trading, so it wins over an older snapshot.
            if selection and not positions and not item.state_data.get("legs") and entry_check:
                selection=None
            row["option_execution"]={
                "model_version":selection.model_version if selection else None,
                "selection_provider":selection.provider if selection else settings.OPTION_CHAIN_PROVIDER,
                "last_entry_check":entry_check,
                "entry_threshold":item.parameters.get("rps_threshold"),
                "iv_score_threshold":item.parameters.get("rps_iv_score_threshold"),
                "selection_evidence":(
                    selection.source_snapshot.get("selection_evidence", {}) if selection
                    else (entry_check or {}).get("selection_evidence") or (
                        {} if entry_check else item.state_data.get("selection_evidence", {})
                    )
                ),
                "status":"WAITING_FOR_OPTION_QUOTES" if quote_job else item.state,
                "readiness_reason":(
                    (quote_job.error_details or {}).get("message") if quote_job else None
                ),
                "selected_strategy":(
                    selection.selected_strategy if selection else item.state_data.get("selected_strategy")
                ),
                "entry_score":selection.entry_score if selection else (
                    (entry_check or {}).get("entry_score") or item.state_data.get("entry_score")
                ),
                "entry_score_scale":rps_engine.WEIGHT_SCALE,
                "entry_score_weights":rps_engine.WEIGHTS,
                "entry_score_components":(
                    (selection.source_snapshot.get("rps_observation") or {}).get("component_scores")
                    if selection else (entry_check or {}).get("entry_score_components")
                ),
                "entry_signal":(
                    (selection.source_snapshot.get("selection_evidence") or {}).get("entry_signal")
                    if selection else None
                ),
                "exit_evaluation":item.state_data.get("exit_evaluation"),
                "observed_at":selection.observed_at if selection else (entry_check or {}).get("evaluated_at") or None,
                "expiration":selection.expiration if selection else item.state_data.get("expiration"),
                "atr_reference":selection.atr_reference if selection else None,
                "atr_value":selection.atr_value if selection else None,
                "atr_multiplier":str(rps_engine.ATR_MULTIPLIER),
                "atr_support":selection.atr_support if selection else (entry_check or {}).get("atr_support"),
                "atr_resistance":selection.atr_resistance if selection else (entry_check or {}).get("atr_resistance"),
                "oi_support":selection.oi_support if selection else (entry_check or {}).get("oi_support"),
                "oi_resistance":selection.oi_resistance if selection else (entry_check or {}).get("oi_resistance"),
                "spot":selection.spot if selection else (entry_check or {}).get("spot"),
                "candidate_rankings":selection.candidate_rankings if selection else [],
                "legs":[{
                    **evidence_by_instrument.get(position.instrument_id, {}),
                    "instrument_id":position.instrument_id,
                    "symbol":position.instrument.symbol,
                    "conid":getattr(position.instrument.broker_contract,"conid",None),
                    "quantity":position.quantity,
                    "right":position.instrument.option_contract.right,
                    "strike":position.instrument.option_contract.strike,
                    "expiration":position.instrument.option_contract.expiration,
                    "multiplier":position.instrument.option_contract.multiplier,
                } for position in positions] or evidence_legs,
            }
    return row


def _get(pk):
    return _strategy_queryset(detail=True).get(pk=pk)


def _intelligence_advisory(instance,trigger):
    """Additive: advisory context for the operator (never blocks anything). None on any problem."""
    try:
        from apps.intelligence.services.advisory import advisory_for_instance
        return advisory_for_instance(instance,trigger)
    except Exception:
        return None


def _intelligence_intent_note(instance,payload,trigger="CREATE",request=None):
    """Additive: hand the operator's request (selections, optional note) to the intelligence journal. Never raises."""
    try:
        from apps.intelligence.services.capture import record_request
        record_request(instance,payload,trigger,actor=_request_actor(request) if request is not None else None,
            idempotency_key=request.headers.get("Idempotency-Key") if request is not None else None)
    except Exception:
        pass


def _request_actor(request):
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        return user.get_username()
    return "frontend_operator"


@csrf_exempt
def instances(request, instance_id=None, options_only=False):
    try:
        if options_only and instance_id and get_plugin(_get(instance_id).definition).strategy_kind != "OPTIONS":
            raise StrategyInstance.DoesNotExist("Option strategy instance not found")
        if request.method == "GET":
            if instance_id:return response(_instance(_get(instance_id),True))
            query=_strategy_queryset()
            if options_only:
                query=query.filter(definition__key__in=[*OPTION_TEMPLATES, AUTO_OPTION_STRATEGY_KEY])
            for field,param in [("portfolio_id","portfolio"),("instrument__symbol","ticker"),("definition__key","strategy_type"),("state","state"),("execution_mode","execution_mode")]:
                if request.GET.get(param):query=query.filter(**{field:request.GET[param]})
            return response([_instance(x) for x in query.order_by("name")])
        try:
            payload=json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            error=StrategyDeletionError("INVALID_STRATEGY_DELETE_REQUEST","Request body must be valid JSON",status=400)
            if request.method=="DELETE" and instance_id:
                audit_strategy_deletion_rejection(instance_id,attempt_key=request.headers.get("Idempotency-Key"),
                    actor=_request_actor(request),error=error)
                raise error
            raise ValueError("Request body must be valid JSON")
        if request.method == "DELETE" and instance_id:
            result=delete_strategy_instance(instance_id,payload.get("strategy_name"),
                attempt_key=request.headers.get("Idempotency-Key"),actor=_request_actor(request))
            return response(result)
        if options_only:
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            if request.method == "POST":
                payload.setdefault("definition_key", AUTO_OPTION_STRATEGY_KEY)
                payload.setdefault("timeframe", "1d")
                if str(payload.get("ticker", "")).upper() == "NIFTY":
                    if payload.get("exchange", "NSE") == "NSE" and payload.get("currency", "INR") == "INR":
                        payload["ticker"] = "NIFTY50"
                    payload.setdefault("asset_class", "IND")
                    payload.setdefault("exchange", "NSE")
                    payload.setdefault("currency", "INR")
            if payload.get("definition_key", AUTO_OPTION_STRATEGY_KEY).upper() not in {*OPTION_TEMPLATES, AUTO_OPTION_STRATEGY_KEY}:
                raise ValueError("This endpoint accepts only option strategies")
            if "execution_mode" in payload:
                raise ValueError("Option execution mode is derived from the portfolio Gateway session; omit execution_mode")
        if request.method == "POST":
            if not isinstance(payload.get("activate", False), bool):
                raise ValueError("activate must be a boolean")
            portfolio=TradingPortfolio.objects.select_related("gateway_session").get(
                pk=payload["portfolio_id"]
            )
            risk=StrategyRiskPolicy.objects.get(pk=payload["risk_policy_id"]) if payload.get("risk_policy_id") else None
            order=OrderPolicy.objects.get(pk=payload["order_policy_id"]) if payload.get("order_policy_id") else None
            profiles=_risk_profiles()
            parameters_in,target_in=payload.get("parameters",{}),payload.get("target_configuration",{})
            profiled=bool(profiles and profiles.applies_to(payload["definition_key"]))
            if profiled:parameters_in,target_in=profiles.prepare_create(payload)
            item,qualification=create_instance(name=payload["name"],definition_key=payload["definition_key"],portfolio=portfolio,
                timeframe=payload["timeframe"],parameters=parameters_in,target_configuration=target_in,
                instrument_id=payload.get("instrument_id"),ticker=payload.get("ticker"),risk_policy=risk,order_policy=order,
                execution_mode=payload.get("execution_mode"),exchange=payload.get("exchange","SMART"),
                currency=payload.get("currency","USD"),primary_exchange=payload.get("primary_exchange"),qualify=payload.get("qualify",True),
                asset_class=payload.get("asset_class","STK"))
            if profiles and not profiled:profiles.log_shadow(payload["definition_key"],item.parameters,payload.get("risk_profile"))
            _intelligence_intent_note(item,payload,"CREATE",request)
            activate_requested=payload.get("activate",False)
            if activate_requested:
                request_key=request.headers.get("Idempotency-Key") or str(uuid.uuid4())
                activation_key=(
                    "create-activate:"
                    + hashlib.sha256(request_key.encode("utf-8")).hexdigest()
                )[:128]
                action_hash=canonical_request_hash("strategy_action",{
                    "strategy_instance_id":item.pk,
                    "action":"enable",
                    "payload":{},
                })
                operation=StrategyAction.objects.create(
                    strategy_instance=item,
                    action="enable",
                    idempotency_key=activation_key,
                    request_hash=action_hash,
                )
                try:
                    item=enable_instance(item,action=operation)
                except StrategyActivationError:
                    # Creation succeeded but the durable activation attempt
                    # reached a truthful failed terminal state. Return the
                    # created resource with its exact blocker and retryability.
                    pass
            row=_instance(_get(item.pk),True);row["qualification_command"]=qualification
            advisory=_intelligence_advisory(item,"ACTIVATION" if activate_requested else "CREATE")
            if advisory:row["advisory"]=advisory
            if activate_requested and row["activation_operation"]["status"]=="PROCESSING":
                return response(row,status=202)
            return response(row,status=201)
        if request.method == "PATCH" and instance_id:
            item=_get(instance_id);changes={}
            for key in ["name","timeframe","parameters","target_configuration","execution_mode"]:
                if key in payload:changes[key]=payload[key]
            if payload.get("definition_key"):changes["definition"]=StrategyDefinition.objects.get(key=payload["definition_key"].upper(),enabled=True)
            if payload.get("risk_policy_id") is not None:changes["risk_policy"]=StrategyRiskPolicy.objects.get(pk=payload["risk_policy_id"])
            if payload.get("order_policy_id") is not None:changes["order_policy"]=OrderPolicy.objects.get(pk=payload["order_policy_id"])
            if payload.get("instrument_id") or payload.get("ticker"):
                changes["instrument"]=resolve_instrument(instrument_id=payload.get("instrument_id"),ticker=payload.get("ticker"),
                    exchange=payload.get("exchange","SMART"),currency=payload.get("currency","USD"),qualify=False)[0]
            profiles=_risk_profiles()
            if profiles:profiles.apply_to_changes(item,payload,changes)
            updated=update_instance(item,changes)
            _intelligence_intent_note(updated,payload,"CHANGE",request)
            row=_instance(_get(updated.pk),True)
            advisory=_intelligence_advisory(updated,"CHANGE") if ("parameters" in changes or "definition" in changes) else None
            if advisory:row["advisory"]=advisory
            return response(row)
        return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"Unsupported method","details":{}})
    except StrategyDeletionError as exc:
        return response(status=exc.status,error={"code":exc.code,"message":str(exc),"details":exc.details})
    except GatewayError as exc:
        return response(status=503 if exc.retryable else 400,error={"code":exc.code,"message":str(exc),"details":exc.details})
    except (KeyError,ValueError,IntegrityError,StrategyInstance.DoesNotExist,StrategyDefinition.DoesNotExist,
            TradingPortfolio.DoesNotExist,StrategyRiskPolicy.DoesNotExist,OrderPolicy.DoesNotExist,Instrument.DoesNotExist) as exc:
        status=404 if isinstance(exc,StrategyInstance.DoesNotExist) else 400
        return response(status=status,error={"code":"INVALID_STRATEGY_INSTANCE","message":str(exc),"details":{}})


@csrf_exempt
def action(request, instance_id, action_name):
    if request.method!="POST":return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"POST required","details":{}})
    key=request.headers.get("Idempotency-Key")
    if not key:return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        item=_get(instance_id);payload=json.loads(request.body or b"{}")
        if action_name not in {"enable","pause","flatten"}:
            return response(status=404,error={"code":"NOT_FOUND","message":"Unknown action","details":{}})
        request_hash=canonical_request_hash("strategy_action",{"strategy_instance_id":instance_id,"action":action_name,"payload":payload})
        retry_requested=request.headers.get("Idempotency-Retry","").strip().lower() in {"1","true","yes"}
        with transaction.atomic():
            operation,created=StrategyAction.objects.select_for_update().get_or_create(idempotency_key=key,defaults={
                "strategy_instance":item,"action":action_name,"request_hash":request_hash})
            if not created:
                require_matching_request(operation.request_hash,request_hash)
                if operation.status=="COMPLETED":
                    if operation.result.get("run_id"):
                        run=StrategyRun.objects.get(pk=operation.result["run_id"])
                        return response({"id":run.pk,"status":run.status,"error":run.error,
                            "target_ids":list(run.targets.values_list("pk",flat=True))},status=200)
                    return response(_instance(_get(instance_id),True),status=200)
                if operation.status=="PROCESSING":
                    return response({"action_id":operation.pk,"status":operation.status},status=202)
                if not retry_requested or not operation.retryable:
                    return response(status=409,error={"code":"RETRY_NOT_ALLOWED","message":operation.last_error or "Strategy action is not retryable","details":{}})
                operation.status="PROCESSING";operation.last_error="";operation.retryable=False
                operation.attempt_count+=1;operation.completed_at=None
                operation.save(update_fields=["status","last_error","retryable","attempt_count","completed_at"])
        if action_name=="enable":
            _intelligence_intent_note(item,payload,"ACTIVATION",request)
            item=enable_instance(item,action=operation)
            operation.refresh_from_db()
            row=_instance(_get(item.pk),True)
            row["action_id"]=operation.pk
            row["activation_operation_status"]=operation.status
            advisory=_intelligence_advisory(item,"ACTIVATION")
            if advisory:row["advisory"]=advisory
            return response(row,status=202 if operation.status=="PROCESSING" else 200)
        if action_name=="pause":
            item=pause_instance(item)
            StrategyAction.objects.filter(pk=operation.pk).update(status="COMPLETED",result={"strategy_instance_id":item.pk},
                completed_at=timezone.now(),last_error="",retryable=False)
            return response(_instance(_get(item.pk),True))
        if action_name=="flatten":
            run=flatten_instance(item,event_id=payload.get("event_id"))
            if run.status=="ERROR":raise RuntimeError(run.error or "Strategy evaluation failed")
            if payload.get("reason"):
                from apps.audit.models import AuditEvent
                AuditEvent.objects.get_or_create(idempotency_key=f"audit:strategy:{item.pk}:flatten:{run.pk}",defaults={
                    "event_type":"strategy.flatten.requested","actor":"frontend_operator","aggregate_type":"strategy_instance",
                    "aggregate_id":str(item.pk),"data":{"reason":payload["reason"],"strategy_run_id":run.pk}})
            result={"run_id":run.pk}
            StrategyAction.objects.filter(pk=operation.pk).update(status="COMPLETED",result=result,
                completed_at=timezone.now(),last_error="",retryable=False)
            return response({"id":run.pk,"status":run.status,"error":run.error,"target_ids":list(run.targets.values_list("pk",flat=True))},status=201)
    except IdempotencyConflict as exc:
        return response(status=409,error={"code":"IDEMPOTENCY_CONFLICT","message":str(exc),"details":{}})
    except (ValueError,StrategyInstance.DoesNotExist) as exc:
        if "operation" in locals():
            StrategyAction.objects.filter(pk=operation.pk).update(status="FAILED",last_error=str(exc)[:1000],
                retryable=bool(getattr(exc,"retryable",False)),completed_at=timezone.now())
        retryable=bool(getattr(exc,"retryable",False))
        return response(status=503 if retryable else 400,error={
            "code":"STRATEGY_ACTION_FAILED","message":str(exc),"details":{"retryable":retryable}})
    except Exception as exc:
        if "operation" in locals():
            StrategyAction.objects.filter(pk=operation.pk).update(status="FAILED",last_error=str(exc)[:1000],
                retryable=True,completed_at=timezone.now())
        return response(status=503,error={"code":"STRATEGY_ACTION_FAILED","message":str(exc),"details":{"retryable":True}})


def related(request, instance_id, resource):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    try:item=_get(instance_id)
    except StrategyInstance.DoesNotExist:return response(status=404,error={"code":"NOT_FOUND","message":"Strategy instance not found","details":{}})
    if resource=="state":return response(_instance(item,True))
    if resource=="runs":return response([{"id":x.pk,"version":x.strategy_version.version if x.strategy_version else None,
        "status":x.status,"triggering_event_id":x.triggering_event_id,"source_data_version":x.source_data_version,
        "started_at":x.started_at,"completed_at":x.completed_at,"error":x.error} for x in item.runs.order_by("-started_at")[:100]])
    if resource=="signals":return response([{"id":x.pk,"run_id":x.run_id,"version":x.strategy_version.version,
        "signal_type":x.signal_type,"signal_time":x.signal_time,"reason":x.reason,"details":x.details} for x in item.signals.order_by("-signal_time")[:100]])
    if resource=="targets":return response([{"id":x.pk,"run_id":x.run_id,"version":x.strategy_version.version if x.strategy_version else None,
        "target_type":x.target_type,"target_weight":x.target_weight,"target_value":x.target_value,"target_quantity":x.target_quantity,
        "direction":x.direction,"signal_type":x.signal_type,"signal_time":x.signal_time,"source_event_id":x.source_event_id,
        "instrument_id":x.instrument_id,"metadata":x.metadata,
        "reason":x.reason,"status":x.status} for x in item.targets.order_by("-created_at")[:100]])
    if resource=="execution-timeline":
        return response(execution_workflow(item)["stages"])
    if resource=="ledger":
        from .reporting import strategy_ledger
        return response(strategy_ledger(item))
    if resource=="pnl":
        from .reporting import strategy_pnl
        return response(strategy_pnl(item))
    return response(status=404,error={"code":"NOT_FOUND","message":"Unknown resource","details":{}})


def chart(request, instance_id):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from apps.market_streams.models import IndicatorValue, MarketBar
    try:
        item=_get(instance_id)
    except StrategyInstance.DoesNotExist:
        return response(status=404,error={"code":"NOT_FOUND","message":"Strategy instance not found","details":{}})
    raw_bars=list(MarketBar.objects.filter(instrument=item.instrument,interval=item.timeframe,is_final=True)
        .order_by("-window_end","-version")[:500])
    latest={}
    for bar in raw_bars:
        if bar.bar_id not in latest:latest[bar.bar_id]=bar
    bars=sorted(latest.values(),key=lambda x:x.window_end)
    start=bars[0].window_start if bars else None
    indicators=IndicatorValue.objects.filter(instrument=item.instrument,timeframe=item.timeframe,is_final=True)
    if start:indicators=indicators.filter(event_time__gte=start)
    indicators=indicators.order_by("event_time")[:2000]
    markers=[]
    for signal in item.signals.order_by("signal_time"):
        markers.append({"time":signal.signal_time,"type":"SIGNAL","label":f"Signal {signal.signal_type}"})
    for target in item.targets.order_by("created_at"):
        markers.append({"time":target.created_at,"type":"TARGET","label":f"Target {target.target_weight}","value":target.target_weight})
    for attribution in item.orderintentattribution_set.select_related("order_intent__order").all():
        order=getattr(attribution.order_intent,"order",None)
        if not order:continue
        markers.append({"time":order.created_at,"type":"ORDER","label":f"Order {attribution.order_intent.side} {order.status}","value":order.quantity})
        for fill in order.fills.all():
            markers.append({"time":fill.executed_at,"type":"FILL","label":f"Fill {fill.quantity} @ {fill.price}","value":fill.price})
    from .reporting import atr_bands, chart_trade_markers, strategy_ledger
    ledger=strategy_ledger(item)
    return response({
        "bars":[{"time":bar.window_end,"open":bar.open,"high":bar.high,"low":bar.low,"close":bar.close,
            "volume":bar.volume,"version":bar.version} for bar in bars],
        "indicators":[{"time":value.event_time,"name":value.indicator,"value":value.value} for value in indicators if value.value is not None],
        "markers":sorted(markers,key=lambda x:x["time"]),
        "trade_markers":chart_trade_markers(item,ledger),
        "atr_bands":atr_bands(ledger,bars),
        "source":"POSTGRES_MARKET_AND_EXECUTION_FACTS",
    })


def policies(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    return response({"risk_policies":[{"id":x.pk,"name":x.name,"maximum_weight":x.maximum_weight,"maximum_notional":x.maximum_notional,
        "maximum_quantity":x.maximum_quantity,"allow_short":x.allow_short} for x in StrategyRiskPolicy.objects.filter(enabled=True)],
        "order_policies":[{"id":x.pk,"name":x.name,"order_type":x.order_type,"time_in_force":x.time_in_force,
        "limit_offset_bps":x.limit_offset_bps,"price_collar_bps":x.price_collar_bps,"allow_market_order":x.allow_market_order,
        "replace_after_seconds":x.replace_after_seconds,"maximum_replacements":x.maximum_replacements,
        "cancel_at_session_end":x.cancel_at_session_end,"outside_regular_hours":x.outside_regular_hours} for x in OrderPolicy.objects.filter(enabled=True)]})


def _gateway_failure(exc, *, operation):
    if isinstance(exc, GatewayCommandTimeout):
        status = 504
    elif isinstance(exc, GatewayCommandRejected):
        status = exc.http_status if exc.http_status in {400, 404, 409, 422} else 502
    else:
        status = 503
    details = {
        **getattr(exc, "details", {}),
        "operation": operation,
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    code = getattr(exc, "code", "GATEWAY_UNAVAILABLE")
    if isinstance(exc, GatewaySessionUnavailable):
        code = "BROKER_SESSION_UNAVAILABLE"
    elif isinstance(exc, GatewayRouteError):
        code = "GATEWAY_ROUTE_UNAVAILABLE"
    return response(status=status,error={"code":code,"message":str(exc),"details":details})


def _authoritative_gateway_session(payload):
    portfolio_id=payload.get("portfolio_id")
    if portfolio_id:
        portfolio=TradingPortfolio.objects.select_related("gateway_session").get(pk=portfolio_id)
        if portfolio.gateway_session is None:
            raise ValueError("The portfolio is not assigned to a broker Gateway session")
        return portfolio.gateway_session
    session_id=payload.get("session_id")
    if not session_id:
        raise BrokerSessionRequired(
            "A portfolio-assigned broker Gateway session is required"
        )
    return BrokerGatewaySession.objects.get(pk=session_id)


@csrf_exempt
def resolve(request):
    invalid=method_guard(request,"GET","POST")
    if invalid:return invalid
    try:
        payload=json.loads(request.body or b"{}") if request.method=="POST" else request.GET
        qualification_requested=bool(payload.get("qualify",request.method=="POST"))
        gateway_session=(
            _authoritative_gateway_session(payload)
            if qualification_requested else None
        )
        instrument,contract,command=resolve_instrument(instrument_id=payload.get("instrument_id"),ticker=payload.get("ticker"),
            asset_class=payload.get("asset_class","STK"),exchange=payload.get("exchange","SMART"),currency=payload.get("currency","USD"),
            primary_exchange=payload.get("primary_exchange"),conid=payload.get("conid"),local_symbol=payload.get("local_symbol"),
            description=payload.get("description"),qualify=qualification_requested,gateway_session=gateway_session,
            expiration=payload.get("expiration"),strike=payload.get("strike"),right=payload.get("right"),
            multiplier=payload.get("multiplier"),trading_class=payload.get("trading_class"),
            underlying_conid=payload.get("underlying_conid"))
        option=getattr(instrument,"option_contract",None)
        return response({"instrument_id":instrument.pk,"symbol":instrument.symbol,"asset_class":instrument.asset_class,
            "exchange":instrument.exchange,"currency":instrument.currency,"conid":contract.conid if contract else None,
            "primary_exchange":contract.primary_exchange if contract else None,"qualification_command":command,
            "expiration":option.expiration if option else None,"strike":option.strike if option else None,
            "right":option.right if option else None,"multiplier":option.multiplier if option else instrument.multiplier,
            "trading_class":option.trading_class if option else "","underlying_conid":option.underlying_conid if option else None})
    except GatewayError as exc:
        return _gateway_failure(exc,operation="QUALIFY")
    except (BrokerGatewaySession.DoesNotExist,TradingPortfolio.DoesNotExist):
        return response(status=404,error={"code":"BROKER_SESSION_NOT_FOUND","message":"Broker session not found","details":{}})
    except BrokerSessionRequired as exc:
        return response(status=400,error={
            "code":"BROKER_SESSION_REQUIRED","message":str(exc),
            "details":{"retryable":False},
        })
    except ValueError as exc:
        return response(status=400,error={"code":"INSTRUMENT_RESOLUTION_FAILED","message":str(exc),"details":{}})
    except Exception as exc:
        return response(status=503,error={
            "code":"INSTRUMENT_RESOLUTION_FAILED",
            "message":str(exc),
            "details":{"operation":"QUALIFY","retryable":True},
        })


def search_instruments(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    try:
        session=_authoritative_gateway_session(request.GET)
        raw_types=str(request.GET.get("asset_classes") or "STK,OPT")
        asset_classes=[value.strip().upper() for value in raw_types.split(",") if value.strip()]
        return response(search_broker_instruments(
            request.GET.get("query"),gateway_session=session,asset_classes=asset_classes,
            country=request.GET.get("country") or None,currency=request.GET.get("currency") or None,
        ))
    except (BrokerGatewaySession.DoesNotExist,TradingPortfolio.DoesNotExist):
        return response(status=404,error={"code":"BROKER_SESSION_NOT_FOUND","message":"Broker session not found","details":{}})
    except BrokerSessionRequired as exc:
        return response(status=400,error={
            "code":"BROKER_SESSION_REQUIRED","message":str(exc),
            "details":{"retryable":False},
        })
    except GatewayError as exc:
        return _gateway_failure(exc,operation="SEARCH_CONTRACTS")
    except ValueError as exc:
        return response(status=400,error={"code":"INVALID_INSTRUMENT_SEARCH","message":str(exc),"details":{"minimum_length":2}})
    except Exception as exc:
        return response(status=503,error={
            "code":"INSTRUMENT_SEARCH_FAILED",
            "message":str(exc),
            "details":{"operation":"SEARCH_CONTRACTS","retryable":True},
        })


@csrf_exempt
def instrument_option_chain(request):
    invalid=method_guard(request,"POST")
    if invalid:return invalid
    try:
        payload=json.loads(request.body or b"{}")
        session=_authoritative_gateway_session(payload)
        instrument=Instrument.objects.select_related("broker_contract").get(pk=payload["underlying_instrument_id"])
        return response(option_chain(underlying_instrument=instrument,gateway_session=session))
    except (BrokerGatewaySession.DoesNotExist,TradingPortfolio.DoesNotExist,Instrument.DoesNotExist):
        return response(status=404,error={"code":"OPTION_UNDERLYING_NOT_FOUND","message":"Option underlying or broker session was not found","details":{}})
    except GatewayError as exc:
        return _gateway_failure(exc,operation="OPTION_CHAIN")
    except (json.JSONDecodeError,KeyError,ValueError) as exc:
        return response(status=400,error={"code":"OPTION_CHAIN_FAILED","message":str(exc),"details":{}})


@csrf_exempt
def resolve_option(request):
    invalid=method_guard(request,"POST")
    if invalid:return invalid
    try:
        payload=json.loads(request.body or b"{}")
        session=_authoritative_gateway_session(payload)
        instrument=Instrument.objects.select_related("broker_contract").get(pk=payload["underlying_instrument_id"])
        option,contract,details=qualify_option_contract(
            underlying_instrument=instrument,expiration=payload["expiration"],strike=payload["strike"],
            right=payload["right"],multiplier=payload["multiplier"],trading_class=payload["trading_class"],
            exchange=payload.get("exchange") or "SMART",gateway_session=session,
        )
        return response({"instrument_id":option.pk,"symbol":option.symbol,"asset_class":"OPT",
            "exchange":option.exchange,"currency":option.currency,"conid":contract.conid,
            "primary_exchange":contract.primary_exchange,"qualification_command":None,
            "expiration":details.expiration,"strike":details.strike,"right":details.right,
            "multiplier":details.multiplier,"trading_class":details.trading_class,
            "underlying_conid":details.underlying_conid})
    except (BrokerGatewaySession.DoesNotExist,TradingPortfolio.DoesNotExist,Instrument.DoesNotExist):
        return response(status=404,error={"code":"OPTION_UNDERLYING_NOT_FOUND","message":"Option underlying or broker session was not found","details":{}})
    except GatewayError as exc:
        return _gateway_failure(exc,operation="QUALIFY_OPTION")
    except (json.JSONDecodeError,KeyError,ValueError,IntegrityError) as exc:
        return response(status=400,error={"code":"OPTION_RESOLUTION_FAILED","message":str(exc),"details":{}})
