import hashlib

from django.core.cache import cache

from apps.core.views import response


def throttle_response(request, scope, *, limit, window_seconds=60):
    if limit <= 0:
        return None
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        identity = f"user:{user.pk}"
    else:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        identity = f"ip:{forwarded or request.META.get('REMOTE_ADDR', 'unknown')}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    key = f"request-throttle:{scope}:{digest}"
    if cache.add(key, 1, timeout=window_seconds):
        return None
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_seconds)
        count = 1
    if count <= limit:
        return None
    return response(
        status=429,
        error={
            "code": "RATE_LIMITED",
            "message": f"Too many {scope.replace('_', ' ')} requests; retry later",
            "details": {"limit": limit, "window_seconds": window_seconds},
        },
    )
