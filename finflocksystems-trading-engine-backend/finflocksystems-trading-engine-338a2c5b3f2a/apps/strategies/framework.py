import hashlib
import json
from decimal import Decimal
from django.conf import settings
from django.db import InterfaceError, OperationalError, transaction
from django.utils import timezone
from apps.audit.models import OutboxEvent
from apps.execution.modes import (
    normalize_execution_mode,
    require_live_trading_allowed,
    require_portfolio_execution_mode,
)
from apps.instruments.services import resolve_instrument
from apps.oms.models import Order
from .models import (StrategyAllocation, StrategyAttributedPosition, StrategyDefinition, StrategyInputBinding,
    StrategyInputRequirement, StrategyInstance, StrategyRun, StrategySignal, StrategyTarget, StrategyVersion)
from .plugins import get_plugin
from .plugins.base import EvaluationContext, StreamInput


AUTOMATIC_EXECUTION_STREAMING_DISABLED = "AUTOMATIC_EXECUTION_STREAMING_DISABLED"
ACTIVATION_IN_PROGRESS_STATES = {"ACTIVATING", "SUBSCRIBING"}
EVALUATION_READY_STATES = {
    "READY_WAITING_FOR_LIVE_BAR", "FLAT", "ENTRY_PENDING", "PARTIALLY_LONG", "LONG",
    "EXIT_PENDING", "PARTIALLY_SHORT", "SHORT",
}


class StrategyNotReadyError(ValueError):
    """The instance left an evaluable state before this evaluation ran.

    A distinct type because it is a lifecycle race, not a plugin defect: a
    queued job is claimed while the instance is evaluable and the subscription
    then flaps it to BLOCKED before the locked re-read. Callers classify it as
    stale input so the job fails without driving the instance into the terminal
    ERROR state, which nothing retries.
    """


class StrategyActivationError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = bool(retryable)


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def configuration_snapshot(instance):
    return {"definition":instance.definition.key,"definition_version":instance.definition.version,
        "instrument_id":instance.instrument_id,"portfolio_id":instance.portfolio_id,"timeframe":instance.timeframe,
        "parameters":instance.parameters,"target_configuration":instance.target_configuration,
        "risk_policy_id":instance.risk_policy_id,"order_policy_id":instance.order_policy_id,
        "execution_mode":instance.execution_mode,
        "execution_input_configuration":{
            "average_volume_window":int(getattr(settings,"EXECUTION_AVERAGE_VOLUME_WINDOW",20)),
        }}


def current_version(instance):
    return instance.versions.get(version=instance.version)


def create_instance(*, name, definition_key, portfolio, timeframe, parameters, target_configuration,
                    instrument_id=None, ticker=None, risk_policy=None, order_policy=None, execution_mode=None,
                    exchange="SMART", currency="USD", primary_exchange=None, qualify=True, gateway=None, asset_class="STK"):
    definition = StrategyDefinition.objects.get(key=definition_key.upper(), enabled=True)
    plugin = get_plugin(definition)
    if timeframe not in definition.supported_timeframes:
        raise ValueError(f"Unsupported timeframe {timeframe}")
    parameters = plugin.validate_configuration(parameters, target_configuration)
    if qualify and gateway is None:
        from apps.broker_gateway.client import GatewayClient
        gateway=GatewayClient.for_portfolio(portfolio,require_commands=True)
    instrument, contract, qualification = resolve_instrument(instrument_id=instrument_id, ticker=ticker, exchange=exchange,
        currency=currency, primary_exchange=primary_exchange, qualify=qualify, gateway=gateway, asset_class=asset_class)
    if instrument.asset_class not in definition.supported_asset_types:
        raise ValueError(f"Unsupported asset type {instrument.asset_class}")
    if plugin.requires_option_context and qualify and contract is None:
        # resolve_instrument queues qualification; option create-and-activate
        # must wait for it before the activation preflight checks the identity.
        from apps.instruments.services import record_qualified_contract
        result = gateway.wait_for_command(qualification, operation="QUALIFY")
        if (result.get("asset_class") != instrument.asset_class
                or result.get("symbol") != instrument.symbol
                or result.get("currency") != instrument.currency):
            raise ValueError("IBKR qualified a different underlying than requested")
        contract = record_qualified_contract(instrument, result)
    mode = require_portfolio_execution_mode(
        portfolio,
        execution_mode,
    )
    with transaction.atomic():
        instance = StrategyInstance.objects.create(name=name, definition=definition, portfolio=portfolio, instrument=instrument,
            timeframe=timeframe, parameters=parameters, target_configuration=target_configuration or {}, risk_policy=risk_policy,
            order_policy=order_policy, execution_mode=mode, enabled=False, state="DISABLED",
            block_reason="IBKR contract qualification pending" if not contract else "")
        version = _create_version(instance)
        StrategyAllocation.objects.create(strategy_instance=instance, portfolio=portfolio,
            weight=Decimal(str((target_configuration or {}).get("capital_share", 1))),
            priority=int((target_configuration or {}).get("priority", 100)))
        register_inputs(instance, version)
    return instance, qualification


def _create_version(instance):
    snapshot=configuration_snapshot(instance)
    version=StrategyVersion.objects.create(strategy_instance=instance,version=instance.version,
        configuration_snapshot=snapshot,parameter_hash=_json_hash(snapshot))
    _intelligence_capture(instance,version,"VERSION")
    return version


def _intelligence_capture(instance,version,trigger):
    """Additive hook: record what this version is run with for the advisory intelligence layer. Never raises."""
    try:
        from apps.intelligence.services.capture import capture_run_config
        capture_run_config(instance,version,trigger)
    except Exception:
        pass


@transaction.atomic
def update_instance(instance, changes):
    material={"definition","instrument","timeframe","parameters","target_configuration","risk_policy","order_policy","execution_mode"}
    if "execution_mode" in changes:
        changes["execution_mode"] = require_portfolio_execution_mode(
            instance.portfolio,
            changes["execution_mode"],
        )
    for key,value in changes.items():
        if key in material | {"name"}:
            setattr(instance,key,value)
    if instance.timeframe not in instance.definition.supported_timeframes:raise ValueError(f"Unsupported timeframe {instance.timeframe}")
    if instance.instrument.asset_class not in instance.definition.supported_asset_types:raise ValueError(f"Unsupported asset type {instance.instrument.asset_class}")
    instance.parameters=get_plugin(instance.definition).validate_configuration(instance.parameters,instance.target_configuration)
    if material & set(changes):
        retire_version(instance)
        instance.version += 1
        instance.state="WARMING_UP" if instance.enabled else "DISABLED"
        instance.warmup_progress=0
        instance.warmup_started_at=timezone.now() if instance.enabled else None
        instance.warmup_last_progress_at=instance.warmup_started_at
        instance.subscription_ready_at=None
        instance.warmup_completed_at=None
        instance.ready_waiting_since=None
        instance.first_evaluation_completed_at=None
        instance.execution_active_at=None
    if "instrument" in changes and not hasattr(instance.instrument,"broker_contract"):
        instance.enabled=False;instance.state="BLOCKED";instance.block_reason="Instrument does not have a qualified IBKR contract"
    instance.clean();instance.save()
    if "target_configuration" in changes:
        StrategyAllocation.objects.filter(strategy_instance=instance, portfolio=instance.portfolio).update(
            weight=Decimal(str(instance.target_configuration.get("capital_share", 1))),
            priority=int(instance.target_configuration.get("priority", 100)),
        )
    if material & set(changes):
        version=_create_version(instance);register_inputs(instance,version)
    return instance


def retire_version(instance):
    now=timezone.now()
    StrategyVersion.objects.filter(strategy_instance=instance,version=instance.version,retired_at__isnull=True).update(retired_at=now)
    deactivate_inputs(instance)


def deactivate_inputs(instance):
    bindings=StrategyInputBinding.objects.filter(strategy_instance=instance,active=True)
    requirement_ids=list(bindings.values_list("requirement_id",flat=True));bindings.update(active=False)
    removed=[]
    for requirement in StrategyInputRequirement.objects.filter(pk__in=requirement_ids):
        requirement.active_ref_count=requirement.bindings.filter(active=True).count();requirement.save(update_fields=["active_ref_count","updated_at"])
        if requirement.active_ref_count==0:removed.append(requirement.identity_hash)
    if removed:
        cycle=(instance.effective_to or timezone.now()).isoformat()
        OutboxEvent.objects.get_or_create(idempotency_key=f"strategy-inputs:{instance.pk}:v{instance.version}:deactivate:{cycle}",defaults={
            "topic":"strategy.inputs.v1","event_type":"strategy.inputs.changed","aggregate_type":"strategy_instance",
            "aggregate_id":str(instance.pk),"partition_key":str(instance.instrument_id),
            "correlation_id":instance.workflow_trace_id,
            "payload":{"strategy_instance_id":instance.pk,"trace_id":str(instance.workflow_trace_id),
            "strategy_version":instance.version,"instrument_id":instance.instrument_id,"timeframe":instance.timeframe,
            "requirements":[],"removed_requirement_hashes":removed}})


@transaction.atomic
def register_inputs(instance, version=None):
    from .input_identity import requirement_identity_hash
    version=version or current_version(instance);plugin=get_plugin(instance.definition)
    requirements=[]
    declared_inputs=list(plugin.required_stream_inputs(instance.parameters))
    execution_adv_window=int(getattr(settings,"EXECUTION_AVERAGE_VOLUME_WINDOW",20))
    if getattr(settings,"EXECUTION_REGISTER_ADV_INPUT",True) and not any(
        item.input_type=="INDICATOR"
        and item.name=="average_volume"
        and int((item.parameters or {}).get("window",0))==execution_adv_window
        for item in declared_inputs
    ):
        declared_inputs.append(StreamInput(
            "INDICATOR",
            "average_volume",
            {"window":execution_adv_window},
            warmup_bars=execution_adv_window,
        ))
    for declared in declared_inputs:
        parameters=dict(declared.parameters or {})
        role=declared.role or parameters.get("role","")
        implementation_version=int(declared.implementation_version)
        digest=requirement_identity_hash(input_type=declared.input_type,name=declared.name,role=role,
            parameters=parameters,instrument_id=instance.instrument_id,timeframe=instance.timeframe,
            implementation_version=implementation_version)
        requirement,_=StrategyInputRequirement.objects.get_or_create(identity_hash=digest,defaults={"instrument":instance.instrument,
            "timeframe":instance.timeframe,"input_type":declared.input_type,"name":declared.name,"role":role,
            "parameters":parameters,"implementation_version":implementation_version,
            "required_bar_fields":list(declared.bar_fields),"warmup_bars":declared.warmup_bars})
        binding,_=StrategyInputBinding.objects.get_or_create(strategy_instance=instance,strategy_version=version,requirement=requirement,
            defaults={"active":instance.enabled})
        if binding.active!=instance.enabled:binding.active=instance.enabled;binding.save(update_fields=["active"])
        requirement.active_ref_count=requirement.bindings.filter(active=True).count();requirement.save(update_fields=["active_ref_count","updated_at"])
        requirements.append(requirement)
    if instance.enabled:
        cycle=instance.effective_from.isoformat() if instance.effective_from else "active"
        OutboxEvent.objects.get_or_create(idempotency_key=f"strategy-inputs:{instance.pk}:v{version.version}:{cycle}",defaults={
            "topic":"strategy.inputs.v1","event_type":"strategy.inputs.changed","aggregate_type":"strategy_instance",
            "aggregate_id":str(instance.pk),"partition_key":str(instance.instrument_id),
            "correlation_id":instance.workflow_trace_id,
            "payload":{"strategy_instance_id":instance.pk,"trace_id":str(instance.workflow_trace_id),
            "strategy_version":version.version,"instrument_id":instance.instrument_id,"timeframe":instance.timeframe,
            "requirements":[{"identity_hash":x.identity_hash,"input_type":x.input_type,"name":x.name,"role":x.role,
            "parameters":x.parameters,"implementation_version":x.implementation_version,
            "warmup_bars":x.warmup_bars} for x in requirements],"removed_requirement_hashes":[]}})
    return requirements


def _activation_queryset():
    return StrategyInstance.objects.select_related(
        "definition", "instrument__broker_contract", "portfolio__account",
        "portfolio__gateway_session",
    )


def _activation_failure(instance_id, reason):
    reason=str(reason)[:255]
    with transaction.atomic():
        instance=StrategyInstance.objects.select_for_update().get(pk=instance_id)
        if instance.input_bindings.filter(active=True).exists():
            deactivate_inputs(instance)
        instance.enabled=False
        instance.state="BLOCKED"
        instance.block_reason=reason
        instance.effective_to=timezone.now()
        instance.save(update_fields=["enabled","state","block_reason","effective_to","updated_at"])
    return instance


def _own_market_data_staleness(blocker, instance):
    """Whether this blocker is only the activating strategy's own stale data.

    A strategy that is not running has no subscription delivering data, so it
    is always reported stale -- and requiring fresh data to start it makes
    activation from cold impossible: enable needs readiness, readiness needs
    data, and only enable produces data. Every other strategy's staleness, and
    every other blocker, still blocks.
    """
    if blocker.get("code") != "MARKET_DATA_STALE":
        return False
    stale = (blocker.get("details") or {}).get("strategies") or []
    return bool(stale) and all(row.get("strategy_id") == instance.pk for row in stale)


def _validate_activation(instance):
    mode=require_portfolio_execution_mode(instance.portfolio,instance.execution_mode)
    require_live_trading_allowed(mode)
    if not settings.KAFKA_ENABLED:
        raise StrategyActivationError(AUTOMATIC_EXECUTION_STREAMING_DISABLED)
    preflight_enabled=getattr(settings,"EXECUTION_ACTIVATION_PREFLIGHT_ENABLED",True)
    if preflight_enabled:
        from apps.execution.readiness import collect_execution_readiness
        readiness=collect_execution_readiness()
        blockers=[
            item for item in readiness["blockers"]
            if not _own_market_data_staleness(item, instance)
        ]
        if blockers:
            codes=",".join(sorted({item["code"] for item in blockers}))
            raise StrategyActivationError(
                f"Execution readiness preflight failed: {codes}",
                retryable=True,
            )
    contract=getattr(instance.instrument,"broker_contract",None)
    if not contract or not contract.conid:
        raise StrategyActivationError("Instrument does not have a qualified IBKR contract")
    session=instance.portfolio.gateway_session
    if session is None:
        raise StrategyActivationError("Portfolio does not have a broker Gateway session")
    from apps.broker_gateway.models import BrokerGatewaySession
    if (session.deleted_at or session.status!=BrokerGatewaySession.Status.CONNECTED
            or not session.commands_enabled or not (session.last_gateway_state or {}).get("connected")):
        raise StrategyActivationError("Portfolio broker Gateway session is not connected and command-ready",retryable=True)
    checked_at=session.last_checked_at
    if preflight_enabled and (
        checked_at is None
        or (timezone.now()-checked_at).total_seconds()
        > int(getattr(settings,"GATEWAY_CONNECTIVITY_STALE_SECONDS",30))
    ):
        raise StrategyActivationError(
            "Portfolio broker Gateway connectivity proof is stale",
            retryable=True,
        )
    if not (session.last_gateway_state or {}).get("reconciled") or not instance.portfolio.account.is_reconciled:
        raise StrategyActivationError(
            "Portfolio account and broker Gateway session are not reconciliation-ready",
            retryable=True,
        )
    from apps.reconciliation.models import ReconciliationBreak
    if ReconciliationBreak.objects.filter(
        run__broker_account=instance.portfolio.account,
        material=True,
        resolved=False,
    ).exists():
        raise StrategyActivationError(
            "Portfolio account has unresolved material reconciliation breaks",
            retryable=True,
        )
    if not session.session_accounts.filter(
            broker_account_id=instance.portfolio.account_id,available=True).exists():
        raise StrategyActivationError("Portfolio account is not available through its broker Gateway session")
    if not instance.definition.enabled:
        raise StrategyActivationError("Strategy definition is disabled")
    plugin=get_plugin(instance.definition)
    plugin.validate_configuration(instance.parameters,instance.target_configuration)
    if plugin.requires_option_context and settings.OPTION_CHAIN_PROVIDER not in {"IBKR", "DHAN"}:
        raise StrategyActivationError("OPTION_CHAIN_PROVIDER must be IBKR or DHAN")
    if plugin.requires_option_context and settings.OPTION_CHAIN_PROVIDER == "DHAN":
        if not getattr(settings,"DHAN_OPTIONS_ENABLED",False):
            raise StrategyActivationError("Dhan automatic option data is disabled")
        if not getattr(settings,"DHAN_CLIENT_ID","") or not getattr(settings,"DHAN_ACCESS_TOKEN",""):
            raise StrategyActivationError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for automatic option entry")
    return instance


def _complete_action(action_id, *, status, instance, subscription=None, error="", retryable=False):
    if not action_id:return
    from .models import StrategyAction
    result={"strategy_instance_id":instance.pk,"activation_status":instance.state,
        "trace_id":str(instance.workflow_trace_id)}
    if subscription is not None:
        result.update({"market_subscription_id":subscription.pk,"subscription_state":subscription.state})
    StrategyAction.objects.filter(pk=action_id).update(
        status=status,result=result,last_error=str(error)[:1000],retryable=retryable,
        completed_at=timezone.now() if status!="PROCESSING" else None)


def complete_activation_workflow(instance):
    """Complete durable activation only after the first live evaluation is persisted."""
    from .models import StrategyAction

    result={
        "strategy_instance_id":instance.pk,
        "activation_status":"ACTIVE",
        "trace_id":str(instance.workflow_trace_id),
        "first_evaluation_completed_at":str(instance.first_evaluation_completed_at),
    }
    StrategyAction.objects.filter(
        strategy_instance=instance,
        action="enable",
        status="PROCESSING",
    ).update(
        status="COMPLETED",
        result=result,
        last_error="",
        retryable=False,
        completed_at=timezone.now(),
    )


def fail_activation_workflow(instance, reason, *, retryable=False):
    """Attach a concrete terminal blocker to an in-flight activation attempt."""
    from .models import StrategyAction

    StrategyAction.objects.filter(
        strategy_instance=instance,
        action="enable",
        status="PROCESSING",
    ).update(
        status="FAILED",
        result={
            "strategy_instance_id":instance.pk,
            "activation_status":instance.state,
            "trace_id":str(instance.workflow_trace_id),
        },
        last_error=str(reason)[:1000],
        retryable=bool(retryable),
        completed_at=timezone.now(),
    )


def enable_instance(instance,gateway=None, *, action=None, construction_run_id=None):
    """Durably stage activation; explicit fake clients execute synchronously for integration tests."""
    instance=_activation_queryset().get(pk=instance.pk)
    try:
        _validate_activation(instance)
    except Exception as exc:
        blocked=_activation_failure(instance.pk,exc)
        _complete_action(getattr(action,"pk",None),status="FAILED",instance=blocked,error=exc,
            retryable=bool(getattr(exc,"retryable",False)))
        raise
    with transaction.atomic():
        locked=StrategyInstance.objects.select_for_update().get(pk=instance.pk)
        version_needs_activation=current_version(locked).activated_at is None
        if (locked.enabled and locked.state not in {"BLOCKED","ERROR"}
                and not version_needs_activation):
            _complete_action(getattr(action,"pk",None),status="COMPLETED",instance=locked)
            return locked
        if locked.state=="ACTIVATING":
            return locked
        if not version_needs_activation:
            locked.enabled=False
        locked.state="ACTIVATING"
        locked.block_reason=""
        locked.effective_to=None
        locked.save(update_fields=["enabled","state","block_reason","effective_to","updated_at"])
        action_id=getattr(action,"pk",None)
        if action_id:
            _complete_action(action_id,status="PROCESSING",instance=locked)
        if gateway is None:
            from .tasks import activate_strategy_instance
            transaction.on_commit(lambda: activate_strategy_instance.delay(
                locked.pk,action_id,construction_run_id))
    if gateway is not None:
        return activate_instance(instance.pk,gateway=gateway,action_id=getattr(action,"pk",None),
            construction_run_id=construction_run_id)
    return StrategyInstance.objects.get(pk=instance.pk)


def activate_instance(instance_id, *, gateway=None, action_id=None, construction_run_id=None):
    """Perform idempotent input/subscription activation without holding a DB lock during Gateway I/O."""
    subscription=None
    try:
        instance=_activation_queryset().get(pk=instance_id)
        _validate_activation(instance)
        with transaction.atomic():
            # PostgreSQL cannot lock a row through the nullable outer join used
            # by portfolio__gateway_session. Lock only the strategy row and
            # resolve related objects normally while the transaction is open.
            instance=StrategyInstance.objects.select_for_update().get(pk=instance_id)
            now=timezone.now()
            first_activation=not instance.enabled
            instance.enabled=True
            instance.state="ACTIVATING"
            instance.block_reason=""
            # A retry after pause/block is a new activation interval. Reusing
            # the original timestamps also reuses the strategy-input outbox
            # idempotency key, so Flink can be left with requirements removed
            # by the previous deactivation and silently discard valid bars.
            instance.effective_from=now if first_activation else (instance.effective_from or now)
            instance.effective_to=None
            instance.warmup_started_at=now if first_activation else (instance.warmup_started_at or now)
            instance.warmup_last_progress_at=now if first_activation else (instance.warmup_last_progress_at or now)
            if first_activation:instance.warmup_progress=0
            if first_activation:
                instance.subscription_ready_at=None
                instance.warmup_completed_at=None
                instance.ready_waiting_since=None
                instance.first_evaluation_completed_at=None
                instance.execution_active_at=None
            instance.kill_switch=False
            instance.save(update_fields=["enabled","kill_switch","state","block_reason","effective_from",
                "effective_to","warmup_started_at","warmup_last_progress_at","warmup_progress",
                "subscription_ready_at","warmup_completed_at","ready_waiting_since",
                "first_evaluation_completed_at","execution_active_at","updated_at"])
            version=current_version(instance)
            refresh_warmup_subscription=not first_activation and version.activated_at is None
            requirements=register_inputs(instance,version)
            declared=list(get_plugin(instance.definition).required_stream_inputs(instance.parameters))
            if declared and (not requirements or instance.input_bindings.filter(
                    strategy_version=version,active=True).count()!=len(requirements)):
                raise StrategyActivationError("Enabled strategy has no active current-version input bindings")
            instance.state="SUBSCRIBING"
            instance.save(update_fields=["state","updated_at"])
        from apps.market_streams.subscriptions import reconcile_market_subscription
        subscription=reconcile_market_subscription(
            instance.instrument,instance.timeframe,gateway,
            force=refresh_warmup_subscription,
            gateway_session=instance.portfolio.gateway_session)
        if subscription.state=="ERROR":
            raise StrategyActivationError(
                subscription.last_error or "Market-data subscription setup failed",
                retryable=True)
        StrategyVersion.objects.filter(pk=version.pk,activated_at__isnull=True).update(
            activated_at=timezone.now())
        instance=StrategyInstance.objects.get(pk=instance_id)
        _intelligence_capture(instance,version,"ACTIVATION")
        if subscription.state in {"ACTIVE","DEGRADED"}:
            from apps.market_streams.services import refresh_strategy_warmup_state
            refresh_strategy_warmup_state(instance)
        else:
            StrategyInstance.objects.filter(pk=instance_id).update(state="SUBSCRIBING",block_reason="")
        instance=StrategyInstance.objects.get(pk=instance_id)
        # Subscription setup is not activation completion. The durable action
        # remains PROCESSING through warm-up and the first persisted live
        # evaluation so API clients cannot report an early success.
        _complete_action(action_id,status="PROCESSING",instance=instance,subscription=subscription)
        if construction_run_id:
            from apps.portfolio_construction.services import record_strategy_activation_result
            record_strategy_activation_result(construction_run_id,instance.pk)
        return instance
    except Exception as exc:
        instance=_activation_failure(instance_id,exc)
        if subscription is not None:
            from apps.market_streams.subscriptions import refresh_market_subscription_counts
            refresh_market_subscription_counts(subscription)
        retryable=bool(getattr(exc,"retryable",False))
        _complete_action(action_id,status="FAILED",instance=instance,subscription=subscription,
            error=exc,retryable=retryable)
        if construction_run_id:
            from apps.portfolio_construction.services import record_strategy_activation_result
            record_strategy_activation_result(construction_run_id,instance.pk)
        raise StrategyActivationError(str(exc),retryable=retryable) from exc


def pause_instance(instance,gateway=None):
    with transaction.atomic():
        instance=StrategyInstance.objects.select_for_update().select_related("instrument").get(pk=instance.pk)
        instance.enabled=False;instance.state="PAUSED";instance.effective_to=timezone.now();instance.save(update_fields=["enabled","state","effective_to","updated_at"])
        deactivate_inputs(instance)
        from apps.rebalancing.coordinator import mark_portfolio_for_target_coordination
        mark_portfolio_for_target_coordination(instance.portfolio_id, logical_event_time=instance.effective_to)
    if settings.KAFKA_ENABLED or gateway is not None:
        from apps.market_streams.subscriptions import reconcile_market_subscription
        reconcile_market_subscription(instance.instrument,instance.timeframe,gateway,gateway_session=instance.portfolio.gateway_session)
    return instance


def _latest_target(instance, instrument_id):
    return instance.targets.filter(
        status="ACTIVE", instrument_id=instrument_id,
    ).order_by("-created_at", "-pk").first()


def _target_changed(instance, target_data):
    if (target_data.get("metadata") or {}).get("lifecycle_locked"):
        # Multi-leg option structures intentionally restate every locked leg on
        # each final bar. This refreshes durable execution evidence as one
        # atomic target set and prevents historical expiries leaking forward.
        return True
    instrument = target_data["instrument"]
    latest = _latest_target(instance, instrument.pk)
    if latest is None:
        return True
    return any((
        latest.target_type != target_data["target_type"],
        Decimal(latest.target_weight) != Decimal(target_data.get("target_weight") or 0),
        (Decimal(latest.target_quantity) if latest.target_quantity is not None else None)
        != (Decimal(target_data["target_quantity"]) if target_data.get("target_quantity") is not None else None),
    ))


@transaction.atomic
def evaluate_instance(instance, *, bar, indicators, previous_indicators=None, event_id=None, source_data_version=1,
                      event_time=None, force=False, retry_failed=False):
    instance=StrategyInstance.objects.select_for_update().select_related("definition","instrument","portfolio").get(pk=instance.pk)
    if not force:
        state_ready=instance.state in EVALUATION_READY_STATES
        retrying_error=instance.state=="ERROR" and retry_failed
        if not instance.enabled or not (state_ready or retrying_error):
            raise StrategyNotReadyError("Strategy instance is not ready for evaluation")
    if not bar.get("is_final",True):
        raise ValueError("Strategies evaluate final bars only")
    if str(bar.get("processing_mode","LIVE")).upper()!="LIVE":
        raise ValueError("Historical processing modes cannot mutate live strategy state")
    if str(bar.get("market_data_type","LIVE")).upper()=="DELAYED" and instance.execution_mode!="PAPER":
        raise ValueError("Delayed market data can mutate PAPER strategies only")
    version=current_version(instance);event_id=str(event_id or bar.get("event_id") or bar.get("bar_id") or _json_hash(bar))
    key=f"strategy:{instance.pk}:v{version.version}:{instance.instrument_id}:{instance.timeframe}:{event_id}:{source_data_version}"
    existing=StrategyRun.objects.select_for_update().filter(idempotency_key=key).first()
    if existing and (existing.status!="ERROR" or not retry_failed):return existing
    if existing:
        prior_state=existing.context_snapshot.get("strategy_state")
        if not prior_state:raise ValueError("This legacy failed strategy run does not retain enough state for a safe retry")
        StrategySignal.objects.filter(run=existing).delete();existing.targets.all().delete()
        OutboxEvent.objects.filter(idempotency_key=f"strategy-run:{existing.pk}:completed").delete()
        instance.state=prior_state;instance.state_data=existing.context_snapshot.get("strategy_state_data") or {}
        instance.block_reason="";instance.save(update_fields=["state","state_data","block_reason","updated_at"])
        existing.status="RUNNING";existing.error="";existing.completed_at=None
        existing.save(update_fields=["status","error","completed_at"]);run=existing
    else:
        snapshot=configuration_snapshot(instance);digest=_json_hash({"key":key,"bar":bar,"indicators":indicators})
        run=StrategyRun.objects.create(strategy_instance=instance,strategy_version=version,
            input_hash=digest,idempotency_key=key,triggering_event_id=event_id,source_data_version=source_data_version,
            configuration_snapshot=snapshot,context_snapshot=json.loads(json.dumps(
                {"bar":bar,"indicators":indicators,"previous_indicators":previous_indicators or {},
                "strategy_state":instance.state,"strategy_state_data":instance.state_data},default=str)))
    try:
        with transaction.atomic():
            attributed_positions=tuple(StrategyAttributedPosition.objects.filter(
                strategy_instance=instance,portfolio=instance.portfolio,
            ).exclude(quantity=0).select_related("instrument__option_contract"))
            attributed=next((item for item in attributed_positions if item.instrument_id==instance.instrument_id),None)
            active_orders=tuple(Order.objects.filter(intent__attributions__strategy_instance=instance,
                status__in=["CREATED","RISK_APPROVED","QUEUED","SUBMITTED","ACKNOWLEDGED","PARTIALLY_FILLED"]))
            context=EvaluationContext(
                strategy_instance=instance,strategy_version=version,instrument=instance.instrument,
                bar=bar,indicators=indicators,previous_indicators=previous_indicators or {},
                previous_state=instance.state,state_data=instance.state_data,
                attributed_position=attributed,active_orders=active_orders,
                attributed_positions=attributed_positions,
                event_metadata={"event_id":event_id,"source_data_version":source_data_version},
            )
            plugin=get_plugin(instance.definition);decision=plugin.evaluate(context);when=event_time or timezone.now()
            StrategySignal.objects.create(run=run,strategy_instance=instance,strategy_version=version,signal_type=decision.signal_type,
                signal_time=when,reason=decision.reason,details={"direction":decision.direction,"confidence":str(decision.confidence) if decision.confidence else None})
            target_data=plugin.build_targets(decision,context)
            normalize_execution_mode(instance.execution_mode)
            created_targets=[]
            for item in target_data:
                if not _target_changed(instance,item):
                    continue
                values=dict(item);instrument=values.pop("instrument")
                created_targets.append(StrategyTarget.objects.create(
                    run=run,strategy_instance=instance,strategy_version=version,portfolio=instance.portfolio,
                    instrument=instrument,signal_time=when,source_event_id=event_id,
                    execution_mode=instance.execution_mode,**values,rationale=values["reason"],
                ))
            instance.state=decision.next_state;instance.state_data=decision.state_data
            completed_at=timezone.now()
            instance.first_evaluation_completed_at=instance.first_evaluation_completed_at or completed_at
            if created_targets:
                instance.execution_active_at=instance.execution_active_at or completed_at
            instance.save(update_fields=[
                "state","state_data","first_evaluation_completed_at",
                "execution_active_at","updated_at",
            ])
            complete_activation_workflow(instance)
            run.status="COMPLETED";run.completed_at=timezone.now();run.save(update_fields=["status","completed_at"])
            OutboxEvent.objects.create(topic="strategy.targets.v1",event_type="strategy.evaluated",aggregate_type="strategy_instance",
                aggregate_id=str(instance.pk),partition_key=str(instance.instrument_id),payload={"strategy_run_id":run.pk,
                "strategy_instance_id":instance.pk,"strategy_version":version.version,"signal_type":decision.signal_type,
                "target_ids":list(run.targets.values_list("pk",flat=True)),"execution_mode":instance.execution_mode,
                "trace_id":str(instance.workflow_trace_id)},
                correlation_id=instance.workflow_trace_id,
                idempotency_key=f"strategy-run:{run.pk}:completed")
            from apps.rebalancing.coordinator import mark_portfolio_for_target_coordination
            mark_portfolio_for_target_coordination(instance.portfolio_id, logical_event_time=when)
            construction_run_id=instance.target_configuration.get("construction_run_id")
            if construction_run_id:
                from apps.portfolio_construction.services import record_strategy_activation_result
                record_strategy_activation_result(construction_run_id,instance.pk)
        return run
    except (OperationalError, InterfaceError, ConnectionError, TimeoutError):
        raise
    except Exception as exc:
        run=StrategyRun.objects.get(pk=run.pk)
        instance=StrategyInstance.objects.get(pk=instance.pk)
        run.status="ERROR";run.error=str(exc);run.completed_at=timezone.now();run.save(update_fields=["status","error","completed_at"])
        instance.state="ERROR";instance.block_reason=str(exc)[:255];instance.save(update_fields=["state","block_reason","updated_at"])
        fail_activation_workflow(instance,exc,retryable=bool(getattr(exc,"retryable",False)))
        return run


@transaction.atomic
def flatten_instance(instance, *, event_id=None, event_time=None):
    instance=StrategyInstance.objects.select_for_update().select_related("definition","instrument","portfolio").get(pk=instance.pk)
    version=current_version(instance);event_id=str(event_id or f"manual-flatten-{instance.pk}-v{version.version}")
    key=f"strategy:{instance.pk}:v{version.version}:{instance.instrument_id}:{instance.timeframe}:{event_id}:1"
    run=StrategyRun.objects.filter(idempotency_key=key).first()
    if run:return run
    run=StrategyRun.objects.create(strategy_instance=instance,strategy_version=version,
        input_hash=_json_hash({"key":key}),idempotency_key=key,triggering_event_id=event_id,
        configuration_snapshot=configuration_snapshot(instance),context_snapshot={"operator_action":"FLATTEN"})
    when=event_time or timezone.now()
    StrategySignal.objects.create(run=run,strategy_instance=instance,strategy_version=version,signal_type="SET_TARGET",
        signal_time=when,reason="Operator requested strategy-attributed flat target")
    attributed=list(StrategyAttributedPosition.objects.filter(
        strategy_instance=instance,portfolio=instance.portfolio,
    ).exclude(quantity=0).select_related("instrument"))
    instruments=[row.instrument for row in attributed] or [instance.instrument]
    for instrument in instruments:
        StrategyTarget.objects.create(run=run,strategy_instance=instance,strategy_version=version,portfolio=instance.portfolio,
            instrument=instrument,target_type="FLAT",target_weight=0,target_quantity=0,
            direction="FLAT",signal_type="SET_TARGET",metadata={"exit":True,"operator":True},
            signal_time=when,source_event_id=event_id,execution_mode=instance.execution_mode,
            reason="Operator requested strategy-attributed flat target",
            rationale="Operator requested strategy-attributed flat target")
    instance.state="FLATTEN_REQUESTED";instance.save(update_fields=["state","updated_at"])
    run.status="COMPLETED";run.completed_at=timezone.now();run.save(update_fields=["status","completed_at"])
    OutboxEvent.objects.create(topic="strategy.targets.v1",event_type="strategy.flattened",aggregate_type="strategy_instance",
        aggregate_id=str(instance.pk),partition_key=str(instance.instrument_id),payload={"strategy_run_id":run.pk,
        "strategy_instance_id":instance.pk,"strategy_version":version.version,"target_ids":list(run.targets.values_list("pk",flat=True)),
        "execution_mode":instance.execution_mode,"trace_id":str(instance.workflow_trace_id)},
        correlation_id=instance.workflow_trace_id,
        idempotency_key=f"strategy-run:{run.pk}:completed")
    from apps.rebalancing.coordinator import mark_portfolio_for_target_coordination
    mark_portfolio_for_target_coordination(instance.portfolio_id, logical_event_time=when)
    return run
