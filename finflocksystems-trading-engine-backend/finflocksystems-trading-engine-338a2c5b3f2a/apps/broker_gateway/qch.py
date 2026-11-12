from dataclasses import dataclass
import logging
import os
from urllib.parse import quote

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


class QCHError(RuntimeError):
    def __init__(self, message, *, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class QCHConflict(QCHError):
    pass


class QCHNotFound(QCHError):
    pass


@dataclass(frozen=True)
class QCHContainer:
    id: str
    name: str
    status: str
    raw: dict


class QCHBrokerClient:
    """Client for QCH's app-scoped Sub-container Broker API."""

    def __init__(self, *, api_host=None, app_id=None, service_token=None, http_session=None, timeout=None):
        # Read the process environment for every new client so a rotated,
        # process-injected service token is never copied into application state.
        self.api_host = str(
            api_host if api_host is not None else os.getenv("QCH_API_HOST", settings.QCH_API_HOST)
        ).rstrip("/")
        self.app_id = str(
            app_id if app_id is not None else os.getenv("QCH_APP_ID", settings.QCH_APP_ID)
        ).strip()
        self.service_token = str(
            service_token if service_token is not None else os.getenv("QCH_SERVICE_TOKEN", settings.QCH_SERVICE_TOKEN)
        ).strip()
        if not self.api_host or not self.app_id or not self.service_token:
            raise QCHError("QCH_API_HOST, QCH_APP_ID, and QCH_SERVICE_TOKEN are required")
        self.http = http_session or requests.Session()
        self.timeout = float(timeout or settings.QCH_REQUEST_TIMEOUT_SECONDS)

    @property
    def collection_url(self):
        return f"{self.api_host}/api/apps/{quote(self.app_id, safe='')}/containers"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.service_token}", "Accept": "application/json"}

    @staticmethod
    def _body(response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("data") is not None:
            return body["data"]
        return body

    @staticmethod
    def _container(row):
        row = row or {}
        return QCHContainer(
            id=str(row.get("id") or row.get("container_id") or row.get("containerId") or ""),
            name=str(row.get("name") or row.get("container_name") or row.get("containerName") or ""),
            status=str(row.get("status") or row.get("state") or "UNKNOWN").upper(),
            raw=dict(row),
        )

    def _request(self, method, url, *, operation="request", **kwargs):
        # Never log `headers`: it carries the bearer service token. The URL is
        # an app-scoped identifier and is safe, and it is the one value that
        # tells a 404 from a wrong QCH_APP_ID apart from a genuinely absent
        # child container.
        headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = self.http.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            logger.warning(
                "broker_gateway stage=qch_unreachable operation=%s method=%s url=%s error=%s",
                operation, method, url, exc.__class__.__name__,
            )
            raise QCHError("QCH sub-container broker is unavailable", retryable=True) from exc
        if response.status_code >= 400:
            logger.warning(
                "broker_gateway stage=qch_http_error operation=%s method=%s url=%s http_status=%s",
                operation, method, url, response.status_code,
            )
        if response.status_code == 404:
            if operation == "list":
                # A 404 on the collection says nothing about any individual
                # child: it is an addressing or availability failure, and this
                # one is observed to flap against a healthy container. Raising
                # QCHNotFound here made callers conclude "the child is gone",
                # which flipped a working session to ERROR every few seconds.
                # Only a SUCCESSFUL listing may conclude that a child is absent.
                raise QCHError(
                    f"QCH container listing returned HTTP 404 for {url}; "
                    "verify QCH_APP_ID and the sub-container API path",
                    status_code=404,
                    retryable=True,
                )
            raise QCHNotFound(
                f"QCH child container was not found ({operation})", status_code=404
            )
        if response.status_code == 409:
            raise QCHConflict("QCH child container name already exists", status_code=409)
        if response.status_code >= 400:
            raise QCHError(
                f"QCH sub-container request failed with HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=response.status_code in {408, 425, 429} or response.status_code >= 500,
            )
        return self._body(response)

    def list_containers(self):
        body = self._request("GET", self.collection_url, operation="list")
        if isinstance(body, dict):
            rows = body.get("items", body.get("containers"))
            if rows is None and (body.get("name") or body.get("container_name")):
                rows = [body]
        else:
            rows = body
        containers = [self._container(row) for row in (rows or [])]
        # Names and statuses only. A container row can echo the child's `env`,
        # which holds the IBKR password, the gateway token and the noVNC
        # password, so `raw` must never reach a log.
        logger.info(
            "broker_gateway stage=qch_listing count=%d names=%s",
            len(containers),
            ",".join(sorted(item.name for item in containers)) or "-",
        )
        return containers

    def find_by_name(self, name):
        containers = self.list_containers()
        match = next((item for item in containers if item.name == name), None)
        if match is None:
            # QCH suffixes app container names on deploy. If it does the same
            # to sub-containers, the exact match here can never succeed, and
            # this line is what shows that.
            logger.warning(
                "broker_gateway stage=qch_name_miss expected=%s available=%s",
                name,
                ",".join(sorted(item.name for item in containers)) or "-",
            )
        return match

    def create_container(self, *, name, image, command=None, env=None, network=None):
        for field, value in (("name", name), ("image", image)):
            if not isinstance(value, str) or not value.strip():
                raise QCHError(f"QCH child container {field} is required")
            if "\r" in value or "\n" in value:
                raise QCHError(f"QCH child container {field} must be a single line")
        if env is not None and not isinstance(env, dict):
            raise QCHError("QCH child container env must be a dictionary")
        if network is not None:
            if not isinstance(network, str) or not network.strip():
                raise QCHError("QCH child container network must be non-empty when supplied")
            if "\r" in network or "\n" in network:
                raise QCHError("QCH child container network must be a single line")
        payload = {"name": name, "image": image}
        if command is not None:
            payload["command"] = command
        if env is not None:
            payload["env"] = env
        if network is not None:
            payload["network"] = network
        try:
            body = self._request("POST", self.collection_url, json=payload, operation="create")
        except QCHConflict:
            existing = self.find_by_name(name)
            if existing is None:
                raise
            return existing
        if isinstance(body, dict) and isinstance(body.get("container"), dict):
            body = body["container"]
        return self._container(body)

    def delete_container(self, container_name):
        try:
            encoded_name = quote(str(container_name), safe="")
            self._request("DELETE", f"{self.collection_url}/{encoded_name}", operation="delete")
        except QCHNotFound:
            return False
        return True
