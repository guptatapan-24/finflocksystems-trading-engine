from __future__ import annotations

from django.conf import settings


class ApiSlashCompatibilityMiddleware:
    """
    Internally map slashless API paths to Django's existing
    trailing-slash URL patterns.

    QCH canonicalizes external URLs by removing their trailing slash.
    This middleware avoids another HTTP redirect and preserves request
    methods and bodies for POST, PATCH and DELETE requests.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path_info = request.path_info

        api_prefixes = {"/api/v1"}

        app_base_path = settings.APP_BASE_PATH.rstrip("/")

        if app_base_path:
            api_prefixes.add(
                f"{app_base_path}/api/v1"
            )

        is_api_path = any(
            path_info == prefix
            or path_info.startswith(f"{prefix}/")
            for prefix in api_prefixes
        )

        if is_api_path and not path_info.endswith("/"):
            request.path_info = f"{path_info}/"

            if not request.path.endswith("/"):
                request.path = f"{request.path}/"

        return self.get_response(request)