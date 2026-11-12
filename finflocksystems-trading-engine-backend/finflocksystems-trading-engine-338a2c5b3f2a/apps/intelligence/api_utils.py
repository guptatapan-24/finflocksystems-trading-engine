"""Shared helpers for intelligence endpoints (R7). Views are NOT csrf_exempt and are open to any caller."""
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.http import JsonResponse

from apps.core.idempotency import canonical_request_hash
from apps.core.views import response


def is_staff(request):
    """The intelligence API is open to everyone: no staff or admin account is needed."""
    return True


def staff_required(view):
    """Kept so existing views read as before; it no longer restricts access."""
    return view


def error(status, code, message, details=None):
    return response(status=status, error={"code": code, "message": message, "details": details or {}})


def envelope(data=None, error=None, meta=None):
    return {"ok": error is None, "data": data if error is None else None, "error": error, "meta": meta or {}}


def json_body(request):
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def run_idempotent(request, operation, payload, handler):
    """Run `handler() -> (status, data)` at most once per Idempotency-Key (R4, R7).

    Same key and same request replays the stored response; same key and a different request is a 409. A handler
    may raise an exception that has `status`, `code` and a message (SuggestionError): the error is stored and
    replayed too. Any other exception removes the record so the caller can retry.
    """
    from .models import IdempotentRequest

    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 150:
        return error(400, "IDEMPOTENCY_KEY_REQUIRED", "A valid Idempotency-Key header is required")
    scoped = f"{operation}:{key}"
    digest = canonical_request_hash(operation, payload)
    created = False
    try:
        with transaction.atomic():  # savepoint: a duplicate key must not poison an enclosing transaction
            record = IdempotentRequest.objects.create(key=scoped, request_hash=digest)
        created = True
    except IntegrityError:
        record = IdempotentRequest.objects.get(key=scoped)
    if not created:
        if record.request_hash != digest:
            return error(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different request")
        if record.status_code == 0:
            return error(409, "REQUEST_IN_PROGRESS", "The first request with this key is still running")
        return JsonResponse(record.response, status=record.status_code, safe=False)
    try:
        status, data = handler()
        body = envelope(data)
    except Exception as exc:
        if not hasattr(exc, "code") or not hasattr(exc, "status"):
            IdempotentRequest.objects.filter(pk=record.pk).delete()
            raise
        status = exc.status
        body = envelope(error={"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})})
    body = json.loads(json.dumps(body, cls=DjangoJSONEncoder))  # datetimes etc. must survive storage and replay
    IdempotentRequest.objects.filter(pk=record.pk).update(status_code=status, response=body)
    return JsonResponse(body, status=status, safe=False)
