from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from apps.core.views import method_guard, response, _serialize
from .models import DeadLetterEvent, ReplayRequest, StreamHealthMetric


def health(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from apps.audit.models import OutboxEvent
    from apps.market_streams.models import InstrumentMarketState, MarketDataProviderTransition, MarketDataSubscription
    metric_objects=list(StreamHealthMetric.objects.all())
    metrics = _serialize(metric_objects, ["component","metric","status","value","observed_at"])
    heartbeat=next((item for item in metric_objects if item.component=="backend-market-consumer" and item.metric=="heartbeat"),None)
    consumer={"status":"UNKNOWN","last_heartbeat":None,"value":{}}
    if heartbeat:
        age=(timezone.now()-heartbeat.observed_at).total_seconds()
        status=heartbeat.status if age<=settings.MARKET_CONSUMER_HEARTBEAT_STALE_SECONDS else "STALE"
        consumer={"status":status,"last_heartbeat":heartbeat.observed_at,"age_seconds":round(age,1),"value":heartbeat.value}
        for row in metrics:
            if row["component"]=="backend-market-consumer" and row["metric"]=="heartbeat":row["status"]=status
    flink = {"status":"UNKNOWN"}
    try:
        import requests
        value = requests.get(settings.FLINK_REST_URL + "/jobs/overview", timeout=2).json()
        jobs=value.get("jobs",[])
        flink = {"status":"HEALTHY" if jobs and all(job.get("state")=="RUNNING" for job in jobs) else "DEGRADED", "jobs": jobs}
    except Exception as exc:
        flink = {"status":"DEGRADED" if settings.KAFKA_ENABLED else "DISABLED", "error":str(exc)[:120]}
    from apps.strategies.models import StrategyInstance
    from apps.market_streams.health import annotate_stream_health, strategy_stream_status
    strategy_query=annotate_stream_health(StrategyInstance.objects.filter(enabled=True).select_related(
        "definition","instrument__broker_contract").order_by("name"))
    strategies=[strategy_stream_status(item) for item in strategy_query]
    gateway_metric=next((item for item in metric_objects if item.component=="gateway" and item.metric=="connectivity"),None)
    gateway={"status":gateway_metric.status if gateway_metric else "UNKNOWN",
        "value":gateway_metric.value if gateway_metric else {},"observed_at":gateway_metric.observed_at if gateway_metric else None}
    kafka_metric=next((item for item in metric_objects if item.component=="kafka" and item.metric=="connectivity"),None)
    lag_metric=next((item for item in metric_objects if item.component=="backend-market-consumer" and item.metric=="topic_lag"),None)
    reasons=[]
    if not settings.KAFKA_ENABLED:reasons.append("Kafka is disabled")
    elif not kafka_metric or kafka_metric.status!="HEALTHY":reasons.append("Kafka connectivity is not healthy")
    if gateway["status"]!="HEALTHY":reasons.append("Gateway is not connected and reconciled")
    if flink["status"]!="HEALTHY":reasons.append("One or more Flink jobs are not running")
    if consumer["status"]!="HEALTHY":reasons.append("Backend market consumer heartbeat is not healthy")
    if lag_metric and lag_metric.status=="DEGRADED":reasons.append("Kafka consumer lag exceeds the configured threshold")
    broken=[item for item in strategies if item["status"]!="HEALTHY"]
    if broken:reasons.append(f"{len(broken)} active strategy data path(s) are not healthy")
    outbox_pending=OutboxEvent.objects.exclude(status="PUBLISHED").count()
    outbox_failed=OutboxEvent.objects.filter(status="FAILED").count()
    if outbox_failed:reasons.append(f"{outbox_failed} outbox event(s) failed publication")
    data_path_status="DISABLED" if not settings.KAFKA_ENABLED else ("HEALTHY" if not reasons else "DEGRADED")
    provider_subscriptions=[{"instrument_id":item.instrument_id,"symbol":item.instrument.symbol,
        "timeframe":item.timeframe,"state":item.state,"active_provider":item.active_provider,
        "fallback_state":item.fallback_state,"fallback_reason":item.fallback_reason,
        "provider_generation":item.provider_generation,"last_primary_event_at":item.last_primary_event_at,
        "last_fallback_event_at":item.last_fallback_event_at} for item in
        MarketDataSubscription.objects.filter(consumer_count__gt=0).select_related("instrument").order_by("instrument__symbol","timeframe")]
    return response({"kafka_enabled":settings.KAFKA_ENABLED,"data_path_status":data_path_status,
        "data_path_reasons":reasons,"gateway":gateway,"consumer":consumer,"metrics":metrics,"flink":flink,
        "strategies":strategies,"outbox_pending":outbox_pending,"outbox_failed":outbox_failed,
        "dead_letter_count":DeadLetterEvent.objects.count(),
        "stale_instrument_count":InstrumentMarketState.objects.exclude(status="FRESH").count(),
        "market_data_providers":provider_subscriptions,
        "provider_transition_count":MarketDataProviderTransition.objects.count()})


def prometheus_metrics(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
    from apps.audit.models import OutboxEvent
    from apps.market_streams.models import InstrumentMarketState, MarketDataProviderTransition, MarketDataSubscription
    registry=CollectorRegistry()
    Gauge("finflock_outbox_pending","Outbox events awaiting Kafka",registry=registry).set(OutboxEvent.objects.exclude(status="PUBLISHED").count())
    Gauge("finflock_dead_letter_total","Persisted dead-letter events",registry=registry).set(DeadLetterEvent.objects.count())
    Gauge("finflock_stale_instruments","Stale or unavailable instruments",registry=registry).set(InstrumentMarketState.objects.exclude(status="FRESH").count())
    provider_gauge=Gauge("finflock_market_data_active_subscriptions","Active subscriptions by provider",["provider"],registry=registry)
    for provider in ("IBKR","FINNHUB","NONE"):
        provider_gauge.labels(provider=provider).set(MarketDataSubscription.objects.filter(consumer_count__gt=0,active_provider=provider).count())
    Gauge("finflock_market_data_provider_transitions_total","Persisted provider transitions",registry=registry).set(MarketDataProviderTransition.objects.count())
    event_gauge=Gauge("finflock_market_data_events_total","Persisted provider event counters",["outcome","provider"],registry=registry)
    for metric in StreamHealthMetric.objects.filter(component="market-data-provider"):
        for provider,total in (metric.value.get("providers") or {}).items():
            event_gauge.labels(outcome=metric.metric,provider=provider).set(total)
    websocket_metric=StreamHealthMetric.objects.filter(component="finnhub-websocket",metric="connection").first()
    reconnects=Gauge("finflock_finnhub_websocket_reconnects","Finnhub WebSocket reconnect count",registry=registry)
    trade_drops=Gauge("finflock_finnhub_trade_aggregation_total","Finnhub trade aggregation counters",["outcome"],registry=registry)
    if websocket_metric:
        reconnects.set((websocket_metric.value or {}).get("reconnect_count",0))
        for outcome,total in ((websocket_metric.value or {}).get("trade_aggregation") or {}).items():
            trade_drops.labels(outcome=outcome).set(total)
    from django.db.models import Max
    from django.utils import timezone
    from apps.audit.models import AuditEvent
    from apps.research.models import (
        InstrumentFeatureSnapshot, RecommendationBatchRun, RecommendationCacheSnapshot,
        ResearchCandidateScore, ResearchDataCoverageSummary, ResearchExperiment, ResearchStrategyImplementation,
        ResearchUniverseMember,
    )
    Gauge("finflock_research_universe_members_mapped", "Active recommendation members mapped to instruments", registry=registry).set(
        ResearchUniverseMember.objects.filter(universe__active=True, active=True, instrument__isnull=False).count()
    )
    coverage_gauge = Gauge("finflock_research_coverage_members", "Member coverage by research dataset", ["dataset"], registry=registry)
    for field, label in (("daily_bar_count", "daily"), ("intraday_bar_count", "intraday"),
                         ("fundamental_fact_count", "fundamentals"), ("event_count", "events")):
        coverage_gauge.labels(dataset=label).set(ResearchDataCoverageSummary.objects.filter(**{f"{field}__gt": 0}).count())
    feature_latest = InstrumentFeatureSnapshot.objects.aggregate(value=Max("available_at"))["value"]
    Gauge("finflock_research_feature_age_seconds", "Age of newest common feature snapshot", registry=registry).set(
        max(0, (timezone.now() - feature_latest).total_seconds()) if feature_latest else 0
    )
    Gauge("finflock_research_implementations_registered", "Registered strategy implementations", registry=registry).set(
        ResearchStrategyImplementation.objects.count()
    )
    Gauge("finflock_research_experiments_completed", "Completed research experiments", registry=registry).set(
        ResearchExperiment.objects.filter(status="COMPLETED").count()
    )
    score_latest = ResearchCandidateScore.objects.aggregate(value=Max("as_of_date"))["value"]
    Gauge("finflock_research_score_age_seconds", "Age of newest candidate score", registry=registry).set(
        max(0, (timezone.localdate() - score_latest).days * 86400) if score_latest else 0
    )
    cache_latest = RecommendationCacheSnapshot.objects.aggregate(value=Max("created_at"))["value"]
    Gauge("finflock_recommendation_cache_age_seconds", "Age of newest recommendation cache", registry=registry).set(
        max(0, (timezone.now() - cache_latest).total_seconds()) if cache_latest else 0
    )
    tier_gauge = Gauge("finflock_recommendation_fallback_snapshots", "Recommendation snapshots by fallback tier", ["tier"], registry=registry)
    for tier in range(1, 6):
        tier_gauge.labels(tier=str(tier)).set(RecommendationCacheSnapshot.objects.filter(fallback_tier=tier).count())
    completed_batches = RecommendationBatchRun.objects.filter(status="COMPLETED", started_at__isnull=False, completed_at__isnull=False)
    latencies = [(item.completed_at - item.started_at).total_seconds() for item in completed_batches.only("started_at", "completed_at")[:1000]]
    Gauge("finflock_recommendation_latency_seconds", "Mean completed recommendation batch latency", registry=registry).set(
        sum(latencies) / len(latencies) if latencies else 0
    )
    substitutions = sum(int((metrics or {}).get("qualification_substitutions") or 0) for metrics in completed_batches.values_list("metrics", flat=True))
    Gauge("finflock_recommendation_qualification_substitutions", "Finalist contract substitutions", registry=registry).set(substitutions)
    failure_gauge = Gauge("finflock_research_provider_failures", "Recorded provider failures by pipeline stage", ["stage"], registry=registry)
    failures = {}
    for data in AuditEvent.objects.filter(event_type="research.provider.failure").values_list("data", flat=True):
        stage = str((data or {}).get("stage") or "unknown")
        failures[stage] = failures.get(stage, 0) + 1
    for stage, total in failures.items():
        failure_gauge.labels(stage=stage).set(total)
    return HttpResponse(generate_latest(registry),content_type=CONTENT_TYPE_LATEST)


def topics(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    # This used to read streaming/kafka/topics.yml, resolved from
    # `Path(settings.BASE_DIR).parent` -- one level above the project, so "/"
    # in a container. Neither that file nor a streaming/kafka directory has
    # ever existed here, so the endpoint always answered with an empty list.
    # The topic set is configuration, and EXECUTION_REQUIRED_KAFKA_TOPICS is
    # the same list scripts/ensure_kafka_topics.py creates and execution
    # readiness checks for, so this reports what the stack actually runs on.
    return response([{"name": topic} for topic in settings.EXECUTION_REQUIRED_KAFKA_TOPICS])


def consumer_lag(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    metrics = StreamHealthMetric.objects.filter(metric__icontains="lag")
    return response(_serialize(metrics,["component","metric","status","value","observed_at"]))


def dead_letter(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    return response(_serialize(DeadLetterEvent.objects.order_by("-created_at")[:250],
        ["event_id","source_topic","consumer_name","reason","envelope","replayed_at","created_at"]))


def replay_status(request,replay_id):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    try:item=ReplayRequest.objects.get(pk=replay_id)
    except ReplayRequest.DoesNotExist:
        return response(status=404,error={"code":"NOT_FOUND","message":"Replay request not found","details":{}})
    return response({"id":item.pk,"topic":item.topic,"consumer_name":item.consumer_name,"status":item.status,
        "processed_count":item.processed_count,"from_timestamp":item.from_timestamp,"to_timestamp":item.to_timestamp,
        "created_at":item.created_at,"completed_at":item.completed_at})


@csrf_exempt
def replay(request):
    if request.method != "POST": return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"POST required","details":{}})
    import json
    key=request.headers.get("Idempotency-Key")
    if not key:return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        payload=json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        unknown=set(payload)-{"topic","consumer_name","from_timestamp","to_timestamp"}
        if unknown:raise ValueError(f"Unsupported replay fields: {', '.join(sorted(unknown))}")
        if not str(payload.get("topic") or "").strip() or not str(payload.get("consumer_name") or "").strip():
            raise ValueError("topic and consumer_name are required")
        from .services import request_replay
        item=request_replay(payload["topic"],payload["consumer_name"],key,payload.get("from_timestamp"),payload.get("to_timestamp"))
        if item.status=="REQUESTED":
            from .tasks import execute_replay_request
            execute_replay_request.delay(item.pk)
        return response({"id":item.pk,"status":item.status},status=202)
    except (json.JSONDecodeError,ValueError,TypeError) as exc:
        return response(status=400,error={"code":"INVALID_REPLAY_REQUEST","message":str(exc),"details":{}})
