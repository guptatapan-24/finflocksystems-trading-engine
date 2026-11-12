from dataclasses import dataclass
import hashlib
import json
import logging
import time

import requests
from django.conf import settings

from .crypto import decrypt_secret


logger = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    def __init__(self, message, *, code="GATEWAY_ERROR", details=None, retryable=False, http_status=None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.retryable = bool(retryable)
        self.http_status = http_status


class GatewayRouteError(GatewayError):
    pass


class GatewaySessionUnavailable(GatewayRouteError):
    pass


class GatewayTransportError(GatewayError):
    def __init__(self, message="Broker gateway request failed", *, details=None):
        super().__init__(
            message,
            code="GATEWAY_TRANSPORT_ERROR",
            details=details,
            retryable=True,
            http_status=503,
        )


class GatewayCommandRejected(GatewayError):
    def __init__(self, message, *, code="GATEWAY_COMMAND_REJECTED", details=None, http_status=None):
        super().__init__(
            message,
            code=code,
            details=details,
            retryable=False,
            http_status=http_status,
        )


class GatewayCommandFailed(GatewayError):
    def __init__(self, message, *, command):
        details = {
            "command_id": command.get("command_id"),
            "command_type": command.get("command_type"),
            "command_status": command.get("status"),
            "attempt_count": command.get("attempt_count", 0),
            "retryable": bool(command.get("retryable")),
            "last_error": command.get("last_error") or message,
        }
        super().__init__(
            message,
            code="GATEWAY_COMMAND_FAILED",
            details=details,
            retryable=details["retryable"],
            http_status=503,
        )
        self.command = command


class GatewayCommandTimeout(GatewayError):
    def __init__(self, message, *, command, timeout):
        details = {
            "command_id": command.get("command_id"),
            "command_type": command.get("command_type"),
            "command_status": command.get("status"),
            "attempt_count": command.get("attempt_count", 0),
            "retryable": True,
            "timeout_seconds": timeout,
            "last_error": command.get("last_error") or "",
        }
        super().__init__(
            message,
            code="GATEWAY_COMMAND_TIMEOUT",
            details=details,
            retryable=True,
            http_status=504,
        )
        self.command = command


@dataclass(frozen=True)
class GatewayRoute:
    session_id: str
    base_url: str
    service_token: str

    def __post_init__(self):
        if not self.session_id or not self.base_url or not self.service_token:
            raise GatewayRouteError("A gateway route requires session identity, URL, and service token")


def route_for_session(gateway_session, *, purpose="read"):
    from .models import BrokerGatewaySession

    if gateway_session is None:
        raise GatewayRouteError("A broker gateway session is required")
    if gateway_session.deleted_at or gateway_session.status in {
        BrokerGatewaySession.Status.STOPPING,
        BrokerGatewaySession.Status.DELETED,
    }:
        raise GatewaySessionUnavailable("The broker gateway session has been deleted or is stopping")
    if purpose == "command":
        connected = (
            gateway_session.status == BrokerGatewaySession.Status.CONNECTED
            and gateway_session.commands_enabled
            and bool((gateway_session.last_gateway_state or {}).get("connected"))
        )
        if not connected:
            raise GatewaySessionUnavailable("Trading commands require a connected and valid broker gateway session")
    elif purpose == "reconnect" and gateway_session.status not in {
        BrokerGatewaySession.Status.STARTING,
        BrokerGatewaySession.Status.WAITING_FOR_LOGIN,
        BrokerGatewaySession.Status.WAITING_FOR_2FA,
        BrokerGatewaySession.Status.CONNECTED,
        BrokerGatewaySession.Status.DISCONNECTED,
        BrokerGatewaySession.Status.LOGIN_FAILED,
        BrokerGatewaySession.Status.ERROR,
    }:
        raise GatewaySessionUnavailable("This broker gateway session is not eligible for reconnect")
    if not gateway_session.internal_base_url or not gateway_session.encrypted_gateway_token:
        raise GatewaySessionUnavailable("The broker gateway session has not been provisioned")
    return GatewayRoute(
        session_id=str(gateway_session.pk),
        base_url=gateway_session.internal_base_url,
        service_token=decrypt_secret(gateway_session.encrypted_gateway_token),
    )


class GatewayClient:
    """Authenticated client bound to one immutable broker-session route."""

    def __init__(self, route, *, http_session=None, require_commands=False, purpose=None):
        self.gateway_session = None
        self.purpose = purpose or ("command" if require_commands else "read")
        if isinstance(route, GatewayRoute):
            resolved = route
        elif hasattr(route, "internal_base_url"):
            self.gateway_session = route
            resolved = route_for_session(route, purpose=self.purpose)
        else:
            raise GatewayRouteError("GatewayClient requires a broker session or explicit GatewayRoute")
        self.route = resolved
        self.base_url = resolved.base_url.rstrip("/")
        self.token = resolved.service_token
        self.http = http_session or requests.Session()

    def _require_session_purpose(self, purpose):
        if self.gateway_session is not None:
            route_for_session(self.gateway_session, purpose=purpose)

    @classmethod
    def for_portfolio(cls, portfolio, *, require_commands=False, http_session=None):
        gateway_session = getattr(portfolio, "gateway_session", None)
        if gateway_session is None:
            raise GatewaySessionUnavailable("Portfolio is not bound to an IBKR gateway session")
        mapping_exists = gateway_session.session_accounts.filter(broker_account_id=portfolio.account_id, available=True).exists()
        if not mapping_exists:
            raise GatewaySessionUnavailable("Portfolio account is not available through its bound gateway session")
        return cls(gateway_session, require_commands=require_commands, http_session=http_session)

    @classmethod
    def for_order(cls, order, *, require_commands=True, http_session=None):
        portfolio = order.intent.portfolio
        return cls.for_portfolio(portfolio, require_commands=require_commands, http_session=http_session)

    def _session_key(self, key):
        return f"session:{self.route.session_id}:{key}"[:255]

    def request(self, method, path, *, idempotency_key=None, retries=2, timeout=None, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
        if idempotency_key:
            headers["Idempotency-Key"] = self._session_key(idempotency_key)
        timeout = float(timeout if timeout is not None else settings.GATEWAY_HTTP_TIMEOUT_SECONDS)
        safe = method.upper() == "GET" or bool(idempotency_key)
        for attempt in range(retries + 1):
            started = time.monotonic()
            logger.info(
                "gateway_http stage=request_start session_id=%s method=%s path=%s attempt=%s timeout_seconds=%s",
                self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1, timeout,
            )
            try:
                response = self.http.request(
                    method,
                    f"{self.base_url}/{path.lstrip('/')}",
                    headers=headers,
                    timeout=timeout,
                    **kwargs,
                )
                if response.status_code >= 500 and safe and attempt < retries:
                    logger.warning(
                        "gateway_http stage=request_retry session_id=%s method=%s path=%s attempt=%s status_code=%s duration_ms=%s",
                        self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1,
                        response.status_code, round((time.monotonic() - started) * 1000),
                    )
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                if 400 <= response.status_code < 500:
                    try:
                        body = response.json()
                        error = body.get("error") or {}
                        message = error.get("message") if isinstance(error, dict) else error
                        code = error.get("code") if isinstance(error, dict) else None
                        details = error.get("details") if isinstance(error, dict) else None
                    except (TypeError, ValueError):
                        message = response.text
                        code = None
                        details = None
                    raise GatewayCommandRejected(
                        str(message or f"Gateway rejected request with HTTP {response.status_code}"),
                        code=str(code or "GATEWAY_COMMAND_REJECTED"),
                        details=details,
                        http_status=response.status_code,
                    )
                response.raise_for_status()
                body = response.json()
                if not body.get("ok", False):
                    error = body.get("error") or {}
                    raise GatewayError(
                        str(error.get("message") if isinstance(error, dict) else error),
                        code=str(error.get("code") or "GATEWAY_ERROR") if isinstance(error, dict) else "GATEWAY_ERROR",
                        details=error.get("details") if isinstance(error, dict) else None,
                    )
                logger.info(
                    "gateway_http stage=request_succeeded session_id=%s method=%s path=%s attempt=%s status_code=%s duration_ms=%s",
                    self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1,
                    response.status_code, round((time.monotonic() - started) * 1000),
                )
                return body.get("data")
            except GatewayError as exc:
                logger.warning(
                    "gateway_http stage=request_rejected session_id=%s method=%s path=%s attempt=%s duration_ms=%s status_code=%s error_code=%s error=%s",
                    self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1,
                    round((time.monotonic() - started) * 1000), exc.http_status or "",
                    exc.code, str(exc),
                )
                raise
            except requests.RequestException as exc:
                if not safe or attempt >= retries:
                    logger.error(
                        "gateway_http stage=request_failed session_id=%s method=%s path=%s attempt=%s duration_ms=%s error_type=%s",
                        self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1,
                        round((time.monotonic() - started) * 1000), type(exc).__name__,
                    )
                    raise GatewayTransportError(
                        "Broker gateway request failed",
                        details={"operation": path.lstrip("/"), "cause": exc.__class__.__name__},
                    ) from exc
                logger.warning(
                    "gateway_http stage=request_retry session_id=%s method=%s path=%s attempt=%s duration_ms=%s error_type=%s",
                    self.route.session_id, method.upper(), path.lstrip("/"), attempt + 1,
                    round((time.monotonic() - started) * 1000), type(exc).__name__,
                )
                time.sleep(0.05 * (2 ** attempt))

    def health(self):
        return self.request("GET", "health/")

    def diagnostics(self):
        # Observability only, on the five-second sync path: one attempt, so a
        # blip costs a log line rather than the retry ladder's backoff.
        return self.request("GET", "diagnostics/", retries=0)

    def session_state(self):
        return self.request("GET", "session/")

    def reconnect(self):
        self._require_session_purpose("reconnect")
        return self.request("POST", "session/reconnect/", json={}, idempotency_key=f"reconnect:{int(time.time())}", retries=0)

    def positions(self):
        return self.request("GET", "positions/")

    def executions(self):
        return self.request("GET", "executions/")

    def accounts(self):
        return self.request("GET", "accounts/")

    def account_summary(self):
        return self.request("GET", "account-summary/")

    def open_orders(self):
        return self.request("GET", "open-orders/")

    def completed_orders(self):
        return self.request("GET", "completed-orders/")

    def order_state(self, internal_id):
        return self.request("GET", f"orders/{internal_id}/state/")

    def command(self, command_id):
        return self.request("GET", f"commands/{int(command_id)}/")

    @staticmethod
    def _operation_timeout(operation):
        name = str(operation or "DEFAULT").upper()
        return float(getattr(
            settings,
            f"GATEWAY_COMMAND_TIMEOUT_{name}_SECONDS",
            settings.GATEWAY_COMMAND_TIMEOUT_DEFAULT_SECONDS,
        ))

    def wait_for_command(self, queued, timeout=None, *, operation=None):
        timeout = float(timeout if timeout is not None else self._operation_timeout(operation))
        command_id = int(queued["command_id"])
        deadline = time.monotonic() + timeout
        current = queued
        fetched = False
        while current.get("status") not in {"COMPLETED", "FAILED", "UNKNOWN"} and time.monotonic() < deadline:
            time.sleep(float(settings.GATEWAY_COMMAND_POLL_INTERVAL_SECONDS))
            current = self.command(command_id)
            fetched = True
        if (
            current.get("status") in {"COMPLETED", "FAILED", "UNKNOWN"}
            and "result" not in current
            and not fetched
        ):
            current = self.command(command_id)
        if current.get("status") in {"FAILED", "UNKNOWN"}:
            raise GatewayCommandFailed(
                current.get("last_error") or f"Gateway command {command_id} failed",
                command=current,
            )
        if current.get("status") != "COMPLETED":
            raise GatewayCommandTimeout(
                f"Gateway command {command_id} timed out after {timeout:g} seconds",
                command=current,
                timeout=timeout,
            )
        return current.get("result") or {}

    def _enqueue_retryable_command(self, path, payload, key):
        queued = self.request(
            "POST", path, json=payload, idempotency_key=key, retries=0
        )
        if queued.get("status") != "FAILED":
            return queued
        current = self.command(int(queued["command_id"]))
        if not current.get("retryable"):
            raise GatewayCommandFailed(
                current.get("last_error") or f"Gateway command {current['command_id']} failed",
                command=current,
            )
        return self.request(
            "POST",
            path,
            json=payload,
            idempotency_key=key,
            retries=0,
            headers={"Idempotency-Retry": "true"},
        )

    def _execute_retryable_command(self, path, payload, key, operation):
        queued = self._enqueue_retryable_command(path, payload, key)
        retries = int(settings.GATEWAY_SAFE_COMMAND_RETRIES)
        for attempt in range(retries + 1):
            try:
                return self.wait_for_command(queued, operation=operation)
            except GatewayCommandFailed as exc:
                if not exc.retryable or attempt >= retries:
                    raise
                queued = self.request(
                    "POST",
                    path,
                    json=payload,
                    idempotency_key=key,
                    retries=0,
                    headers={"Idempotency-Retry": "true"},
                )
        raise AssertionError("unreachable")

    def search_contracts(self, query, *, asset_classes=None, country=None, currency=None):
        self._require_session_purpose("command")
        query = str(query).strip()
        payload = {"query": query}
        if asset_classes:
            payload["asset_classes"] = list(asset_classes)
        if country:
            payload["country"] = str(country).upper()
        if currency:
            payload["currency"] = str(currency).upper()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.casefold().encode()).hexdigest()[:32]
        result = self._execute_retryable_command(
            "contracts/search/",
            payload,
            f"contract-search:{digest}",
            "SEARCH_CONTRACTS",
        )
        return result.get("results", [])

    def option_chain(self, payload):
        self._require_session_purpose("command")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:40]
        result = self._execute_retryable_command(
            "contracts/option-chain/",
            payload,
            f"option-chain:{digest}",
            "OPTION_CHAIN",
        )
        return result

    def option_quotes(self, contracts):
        from uuid import uuid4
        self._require_session_purpose("command")
        return self._execute_retryable_command(
            "options/quotes/", {"contracts": contracts, "mode": self.gateway_session.mode},
            f"option-quotes:{uuid4().hex}", "OPTION_QUOTES",
        )

    def events(self, after=0):
        return self.request("GET", f"events/?after={int(after)}")

    def ack_events(self, sequence):
        return self.request(
            "POST", "events/ack/", json={"sequence": int(sequence)}, idempotency_key=f"events-ack:{int(sequence)}", retries=0
        )

    def place_order(self, payload, key):
        self._require_session_purpose("command")
        return self.request("POST", "orders/", json=payload, idempotency_key=key, retries=0)

    def place_option_order(self, payload, key):
        self._require_session_purpose("command")
        return self.request("POST", "options/orders/", json=payload, idempotency_key=key, retries=0)

    def modify_order(self, internal_id, payload, key):
        self._require_session_purpose("command")
        return self.request("PATCH", f"orders/{internal_id}/", json=payload, idempotency_key=key, retries=0)

    def cancel_order(self, internal_id, key):
        self._require_session_purpose("command")
        return self.request("POST", f"orders/{internal_id}/cancel/", json={}, idempotency_key=key, retries=0)

    def qualify_contract(self, payload, key):
        self._require_session_purpose("command")
        return self._enqueue_retryable_command("contracts/qualify/", payload, key)

    def qualify_contract_exact(self, payload, key):
        self._require_session_purpose("command")
        return self._execute_retryable_command("contracts/qualify/", payload, key, "QUALIFY")

    def historical_bars(self, payload, timeout=None):
        self._require_session_purpose("command")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:40]
        queued = self.request(
            "POST", "market-data/history/", json=payload, idempotency_key=f"historical-data:{digest}", retries=0
        )
        return self.wait_for_command(queued, timeout=timeout, operation="HISTORICAL_DATA")

    def historical_schedule(self, payload, timeout=None):
        self._require_session_purpose("command")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:40]
        queued = self.request(
            "POST", "market-data/schedule/", json=payload, idempotency_key=f"historical-schedule:{digest}", retries=0
        )
        return self.wait_for_command(queued, timeout=timeout, operation="HISTORICAL_SCHEDULE")

    def subscribe_market_data(self, payload, key):
        self._require_session_purpose("command")
        return self.request(
            "POST",
            "market-data/subscriptions/",
            json=payload,
            idempotency_key=key,
            retries=2,
        )

    def cancel_market_data(self, payload, key):
        self._require_session_purpose("command")
        return self.request(
            "POST",
            "market-data/subscriptions/cancel/",
            json=payload,
            idempotency_key=key,
            retries=2,
        )
