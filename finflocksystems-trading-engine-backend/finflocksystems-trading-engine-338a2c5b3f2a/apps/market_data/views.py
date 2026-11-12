import json
from django.conf import settings

from apps.audit.models import AuditEvent
from apps.core.throttling import throttle_response
from apps.core.views import response

from .models import MarketDataProviderConfiguration
from .services import FinnhubClient, FinnhubError, encrypt_api_key, provider_status


def _actor(request):
    user = getattr(request, "user", None)
    return user.get_username() if user and user.is_authenticated else "operator/system"


def status(request):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    from apps.instruments.models import InstrumentProviderMapping
    from apps.market_streams.models import MarketDataProviderTransition, MarketDataSubscription
    data=provider_status()
    data["fallback"]={"enabled":settings.MARKET_DATA_FALLBACK_ENABLED,
        "historical_enabled":settings.FINNHUB_HISTORICAL_FALLBACK_ENABLED,
        "live_enabled":settings.FINNHUB_LIVE_FALLBACK_ENABLED,
        "auto_failback_enabled":settings.FINNHUB_AUTO_FAILBACK_ENABLED,
        "active_subscriptions":MarketDataSubscription.objects.filter(active_provider="FINNHUB",consumer_count__gt=0).count(),
        "verified_mappings":InstrumentProviderMapping.objects.filter(provider="FINNHUB",status="VERIFIED").count(),
        "transition_count":MarketDataProviderTransition.objects.count()}
    return response(data)


def ingestion_status(request):
    """Read-only: what the nightly NFO ingestion has loaded, from its own ledger."""
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    from .ingestion_status import ingestion_status as read_status
    return response(read_status())


def _mapping_row(item):
    return {"id":item.pk,"instrument_id":item.instrument_id,"symbol":item.instrument.symbol,
        "conid":getattr(getattr(item.instrument,"broker_contract",None),"conid",None),"provider":item.provider,
        "provider_symbol":item.provider_symbol,"exchange_mic":item.exchange_mic,
        "provider_exchange":item.provider_exchange,"currency":item.currency,"isin":item.isin,"figi":item.figi,
        "status":item.status,"verification_method":item.verification_method,"verified_at":item.verified_at,
        "last_error":item.last_error,"updated_at":item.updated_at}


def mappings(request, instrument_id=None):
    from apps.instruments.models import Instrument, InstrumentProviderMapping
    if request.method=="GET":
        query=InstrumentProviderMapping.objects.filter(provider="FINNHUB").select_related("instrument__broker_contract")
        if instrument_id:query=query.filter(instrument_id=instrument_id)
        return response([_mapping_row(item) for item in query.order_by("instrument__symbol","instrument_id")])
    if request.method!="POST":
        return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"GET or POST required","details":{}})
    key=request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        payload=json.loads(request.body or b"{}")
        instrument=Instrument.objects.select_related("broker_contract").get(pk=instrument_id)
        from .mapping import manually_verify_finnhub_mapping
        mapping=manually_verify_finnhub_mapping(instrument,payload.get("provider_symbol"))
        AuditEvent.objects.get_or_create(idempotency_key=f"finnhub-mapping:{key}",defaults={
            "event_type":"market_data.mapping.verified","actor":_actor(request),"aggregate_type":"instrument",
            "aggregate_id":str(instrument.pk),"data":{"provider":"FINNHUB","provider_symbol":mapping.provider_symbol,
                "status":mapping.status,"verification_method":mapping.verification_method}})
        return response(_mapping_row(mapping),status=200 if mapping.status=="VERIFIED" else 422)
    except Instrument.DoesNotExist:
        return response(status=404,error={"code":"NOT_FOUND","message":"Instrument not found","details":{}})
    except (ValueError,json.JSONDecodeError,FinnhubError) as exc:
        return response(status=getattr(exc,"status_code",400),error={"code":getattr(exc,"code","INVALID_MAPPING"),
            "message":str(exc),"details":{}})


def configure(request):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key=request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    throttled = throttle_response(
        request,
        "finnhub",
        limit=settings.FINNHUB_OPERATION_THROTTLE_LIMIT,
        window_seconds=settings.EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS,
    )
    if throttled:
        return throttled
    try:
        payload = json.loads(request.body or b"{}")
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("Finnhub API key cannot be empty")
        config, _ = MarketDataProviderConfiguration.objects.get_or_create(provider="FINNHUB")
        config.encrypted_api_key = encrypt_api_key(api_key)
        config.api_key_last_four = api_key[-4:]
        config.enabled = True
        # ponytail: a saved key is meant to be used; default to overriding the environment key.
        config.override_environment = bool(payload.get("override_environment", True))
        config.updated_by = _actor(request)
        config.save(update_fields=["encrypted_api_key", "api_key_last_four", "enabled",
                                   "override_environment", "updated_by", "updated_at"])
        AuditEvent.objects.get_or_create(
            idempotency_key=f"finnhub-config:{key}",
            defaults={
                "event_type": "market_data.credential.updated",
                "actor": _actor(request),
                "aggregate_type": "market_data_provider",
                "aggregate_id": "FINNHUB",
                "data": {"key_replaced": True, "enabled": config.enabled, "override_environment": config.override_environment},
            },
        )
        return response(provider_status())
    except (ValueError, json.JSONDecodeError) as exc:
        return response(status=400, error={"code": "INVALID_FINNHUB_CONFIGURATION", "message": str(exc), "details": {}})


def test(request):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key=request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    throttled = throttle_response(
        request,
        "finnhub",
        limit=settings.FINNHUB_OPERATION_THROTTLE_LIMIT,
        window_seconds=settings.EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS,
    )
    if throttled:
        return throttled
    try:
        payload = json.loads(request.body or b"{}")
        api_key = str(payload.get("api_key") or "").strip()
        result = FinnhubClient(api_key=api_key or None).test_connection(str(payload.get("symbol") or "AAPL").upper())
        AuditEvent.objects.get_or_create(
            idempotency_key=f"finnhub-test:{key}",
            defaults={
                "event_type": "market_data.connection.tested",
                "actor": _actor(request),
                "aggregate_type": "market_data_provider",
                "aggregate_id": "FINNHUB",
                "data": {"connected": True, "source": result["source"]},
            },
        )
        return response({**provider_status(), **result})
    except (FinnhubError, ValueError, json.JSONDecodeError) as exc:
        status_code = exc.status_code if isinstance(exc, FinnhubError) else 400
        AuditEvent.objects.get_or_create(
            idempotency_key=f"finnhub-test:{key}",
            defaults={
                "event_type": "market_data.connection.tested",
                "actor": _actor(request),
                "aggregate_type": "market_data_provider",
                "aggregate_id": "FINNHUB",
                "data": {"connected": False, "code": getattr(exc, "code", "FINNHUB_TEST_FAILED")},
            },
        )
        return response(status=status_code, error={"code": getattr(exc, "code", "FINNHUB_TEST_FAILED"), "message": str(exc), "details": {}})
