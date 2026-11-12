import json
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.core.views import method_guard, response

from .client import GatewayClient, GatewayError
from .configuration import (
    ManagedBrokerGatewayUnavailable,
    managed_broker_deployment_configuration,
    managed_broker_unavailable_error,
)
from .crypto import (
    encrypt_secret,
    generate_novnc_password,
    generate_service_token,
    issue_novnc_access_token,
    mask_username,
)
from .models import BrokerGatewaySession, BrokerGatewaySessionSecret, normalize_broker_mode
from .qch import QCHBrokerClient, QCHError
from .services import container_name_for, delete_session, temporary_secret_expiry
from .tasks import provision_broker_session


CONTAINER_CONFIGURATION_FIELDS = {
    "image",
    "ibkr_gateway_image",
    "registry_username",
    "registry_password",
    "registry_token",
    "registry_auth",
    "docker_config",
    "pull_secret",
    "dockerhub_token",
    "dockerhub_password",
    "dockerhub_username",
    "dockerhub_auth",
    "docker_username",
    "docker_password",
    "docker_token",
}


def _payload(request):
    value = json.loads(request.body or b"{}")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def _bounded(value, field, maximum, *, required=True):
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} cannot exceed {maximum} characters")
    return result


def _market_data_type(payload, mode, current=None):
    """The data type the login is entitled to, from its declared subscriptions.

    Omitted keeps the session's current type, or for a new session the type
    the mode used to imply, so older clients start sessions as before.
    """
    types = BrokerGatewaySession.MarketDataType
    declared = payload.get("market_data_subscription")
    if declared is None:
        result = current or (types.LIVE if mode == BrokerGatewaySession.Mode.LIVE else types.DELAYED)
    elif isinstance(declared, bool):
        result = types.LIVE if declared else types.DELAYED
    else:
        raise ValueError("market_data_subscription must be true or false")
    if mode == BrokerGatewaySession.Mode.LIVE and result != types.LIVE:
        raise ValueError("Live sessions require a real-time IBKR market-data subscription")
    return result


def _reject_container_configuration(payload):
    if {str(name).casefold() for name in payload} & CONTAINER_CONFIGURATION_FIELDS:
        raise ValueError("The Gateway image and registry authentication are configured by the server")


def _managed_broker_preflight():
    configuration = managed_broker_deployment_configuration()
    if configuration["available"]:
        return None
    return response(status=503, error=managed_broker_unavailable_error(configuration))


def _public_novnc_url(request, session):
    if not session.child_container_name or session.status in {session.Status.STOPPING, session.Status.DELETED}:
        return None
    token, _ = issue_novnc_access_token(session.pk)
    base = str(getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base:
        base = request.build_absolute_uri("/").rstrip("/")
    prefix = settings.APP_BASE_PATH.rstrip("/")
    api_path = f"/api/v1/broker-sessions/{session.pk}/novnc"
    public_root = base if not prefix or base.endswith(prefix) else f"{base}{prefix}"
    fragment = f"access_token={quote(token)}"
    return f"{public_root}{api_path}/connect/#{fragment}"


def _account_rows(session):
    rows = []
    mappings = session.session_accounts.select_related("broker_account").order_by("broker_account__account_id")
    for mapping in mappings:
        account = mapping.broker_account
        portfolio = session.portfolios.filter(account=account).order_by("pk").first()
        rows.append({
            "id": account.pk,
            "account_id": account.account_id,
            "alias": mapping.broker_alias or account.alias,
            "base_currency": account.base_currency,
            "net_liquidation": account.net_liquidation,
            "available_cash": account.available_cash,
            "buying_power": account.buying_power,
            "daily_pnl": account.daily_pnl,
            "is_reconciled": account.is_reconciled,
            "kill_switch": account.kill_switch,
            "updated_at": account.updated_at,
            "available": mapping.available,
            "last_seen_at": mapping.last_seen_at,
            "default_portfolio_id": portfolio.pk if portfolio else None,
        })
    return rows


def serialize_session(request, session, *, include_accounts=False):
    accounts = _account_rows(session) if include_accounts else None
    result = {
        "id": str(session.pk),
        "display_name": session.display_name,
        "username_hint": session.username_hint,
        "mode": session.mode,
        "market_data_type": session.market_data_type,
        "status": session.status,
        "connected": session.status == session.Status.CONNECTED,
        "commands_enabled": session.commands_enabled,
        "container_status": session.last_qch_state.get("status", "") if session.last_qch_state else "",
        "account_count": session.session_accounts.filter(available=True).count(),
        "last_error": session.last_error,
        "last_gateway_state": session.last_gateway_state,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "provisioned_at": session.provisioned_at,
        "connected_at": session.connected_at,
        "last_checked_at": session.last_checked_at,
        "deleted_at": session.deleted_at,
        "needs_novnc": session.mode == session.Mode.LIVE and session.status != session.Status.CONNECTED,
        "novnc_url": _public_novnc_url(request, session),
    }
    if include_accounts:
        result["accounts"] = accounts
        result["default_portfolio_id"] = next(
            (item["default_portfolio_id"] for item in accounts if item["available"] and item["default_portfolio_id"]), None
        )
    return result


@csrf_exempt
def sessions(request, session_id=None, action=None):
    if session_id is None:
        invalid = method_guard(request, "GET", "POST")
    elif action in {"reconnect", "credentials"}:
        invalid = method_guard(request, "POST")
    elif action == "accounts":
        invalid = method_guard(request, "GET")
    else:
        invalid = method_guard(request, "GET", "DELETE")
    if invalid:
        return invalid
    try:
        if session_id is None and request.method == "GET":
            queryset = BrokerGatewaySession.objects.all().order_by("-created_at")
            if request.GET.get("include_deleted", "false").lower() != "true":
                queryset = queryset.exclude(status=BrokerGatewaySession.Status.DELETED)
            return response([serialize_session(request, item) for item in queryset])
        if session_id is None:
            unavailable = _managed_broker_preflight()
            if unavailable:
                return unavailable
            payload = _payload(request)
            _reject_container_configuration(payload)
            display_name = _bounded(payload.get("display_name") or "IBKR session", "display_name", 128)
            username = _bounded(payload.get("username"), "username", 128)
            password = _bounded(payload.get("password"), "password", 512)
            mode = normalize_broker_mode(payload.get("mode"))
            market_data_type = _market_data_type(payload, mode)
            gateway_token = generate_service_token()
            novnc_password = generate_novnc_password()
            with transaction.atomic():
                session = BrokerGatewaySession(
                    display_name=display_name,
                    username_hint=mask_username(username),
                    mode=mode,
                    market_data_type=market_data_type,
                    encrypted_gateway_token=encrypt_secret(gateway_token),
                    encrypted_novnc_password=encrypt_secret(novnc_password),
                    child_container_name="pending",
                )
                session.child_container_name = container_name_for(session.pk)
                session.full_clean()
                session.save()
                BrokerGatewaySessionSecret.objects.create(
                    session=session,
                    encrypted_username=encrypt_secret(username),
                    encrypted_password=encrypt_secret(password),
                    expires_at=temporary_secret_expiry(),
                )
                transaction.on_commit(lambda: provision_broker_session.delay(str(session.pk)))
            return response(serialize_session(request, session, include_accounts=True), status=202)

        session = BrokerGatewaySession.objects.get(pk=session_id)
        if action == "accounts":
            return response(_account_rows(session))
        if action == "reconnect":
            command = GatewayClient(session, purpose="reconnect").reconnect()
            BrokerGatewaySession.objects.filter(pk=session.pk).update(
                status=BrokerGatewaySession.Status.STARTING, last_error="", last_checked_at=timezone.now()
            )
            session.refresh_from_db()
            return response({"session": serialize_session(request, session), "gateway_command": command}, status=202)
        if action == "credentials":
            unavailable = _managed_broker_preflight()
            if unavailable:
                return unavailable
            payload = _payload(request)
            _reject_container_configuration(payload)
            username = _bounded(payload.get("username"), "username", 128)
            password = _bounded(payload.get("password"), "password", 512)
            market_data_type = _market_data_type(payload, session.mode, session.market_data_type)
            if session.status in {session.Status.STOPPING, session.Status.DELETED}:
                raise ValueError("Deleted or stopping sessions cannot accept credentials")
            qch = QCHBrokerClient()
            if session.child_container_name:
                qch.delete_container(session.child_container_name)
            with transaction.atomic():
                session = BrokerGatewaySession.objects.select_for_update().get(pk=session.pk)
                if session.status in {session.Status.STOPPING, session.Status.DELETED}:
                    raise ValueError("Deleted or stopping sessions cannot accept credentials")
                BrokerGatewaySessionSecret.objects.update_or_create(
                    session=session,
                    defaults={
                        "encrypted_username": encrypt_secret(username),
                        "encrypted_password": encrypt_secret(password),
                        "expires_at": temporary_secret_expiry(),
                    },
                )
                session.username_hint = mask_username(username)
                # The replacement child is created with this type, so a new
                # login can declare different subscriptions than the old one.
                session.market_data_type = market_data_type
                session.status = session.Status.CREATING
                session.commands_enabled = False
                session.last_qch_state = {}
                session.last_gateway_state = {}
                session.last_error = ""
                session.deleted_at = None
                session.lifecycle_version += 1
                session.save(update_fields=[
                    "username_hint", "market_data_type", "status", "commands_enabled", "last_qch_state",
                    "last_gateway_state", "last_error", "deleted_at", "lifecycle_version", "updated_at",
                ])
                transaction.on_commit(lambda: provision_broker_session.delay(str(session.pk)))
            return response(serialize_session(request, session, include_accounts=True), status=202)
        if request.method == "DELETE":
            unavailable = _managed_broker_preflight()
            if unavailable:
                return unavailable
            session, removed = delete_session(session.pk)
            return response({
                "session": serialize_session(request, session, include_accounts=True),
                "container_deleted": removed,
                "warning": "The gateway container and monitoring stopped and bound strategies were paused. Existing IBKR orders were not automatically cancelled.",
            })
        return response(serialize_session(request, session, include_accounts=True))
    except BrokerGatewaySession.DoesNotExist:
        return response(status=404, error={"code": "BROKER_SESSION_NOT_FOUND", "message": "Broker session was not found", "details": {}})
    except json.JSONDecodeError:
        return response(status=400, error={"code": "INVALID_JSON", "message": "Request body must be valid JSON", "details": {}})
    except (ValueError, ValidationError) as exc:
        return response(status=400, error={"code": "BROKER_SESSION_INVALID", "message": str(exc), "details": {}})
    except ManagedBrokerGatewayUnavailable as exc:
        return response(status=503, error=managed_broker_unavailable_error(exc.configuration))
    except QCHError as exc:
        return response(status=503, error={"code": "QCH_UNAVAILABLE", "message": str(exc), "details": {}})
    except GatewayError as exc:
        return response(status=503, error={"code": "BROKER_SESSION_UNAVAILABLE", "message": str(exc), "details": {}})
