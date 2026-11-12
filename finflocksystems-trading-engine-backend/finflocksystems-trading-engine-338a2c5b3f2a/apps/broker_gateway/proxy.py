import asyncio
from http.cookies import SimpleCookie
import json
import re
import struct
from urllib.parse import parse_qs, urlencode

import httpx
from asgiref.sync import sync_to_async
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from django.conf import settings
from websockets.asyncio.client import connect as websocket_connect

from .crypto import decrypt_secret, validate_novnc_access_token
from .models import BrokerGatewaySession
from .services import CONTAINER_NAME_RE
from .vnc import normalize_vnc_password
import logging

logger = logging.getLogger(__name__)


HOP_HEADERS = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te",
    b"trailers", b"transfer-encoding", b"upgrade",
}


def _normalized_prefix(value):
    value = "/" + str(value or "").strip("/") if str(value or "").strip("/") else ""
    return value if not value or re.fullmatch(r"/[A-Za-z0-9._~/-]+", value) else ""


def _headers(scope):
    return {key.lower(): value for key, value in scope.get("headers", [])}


def _external_prefix(scope):
    configured = _normalized_prefix(settings.APP_BASE_PATH)
    forwarded = _headers(scope).get(b"x-forwarded-prefix", b"").decode("latin-1", "replace").split(",", 1)[0]
    return configured or _normalized_prefix(forwarded)


def _route(scope):
    path = scope.get("path", "")
    routed = path
    prefixes = {_normalized_prefix(settings.APP_BASE_PATH), _external_prefix(scope)} - {""}
    for prefix in sorted(prefixes, key=len, reverse=True):
        if routed == prefix:
            routed = "/"
            break
        if routed.startswith(prefix + "/"):
            routed = routed[len(prefix):]
            break
    match = re.match(
        r"^/api/v1/broker-sessions/(?P<session>[0-9a-fA-F-]{36})/novnc(?:/(?P<asset>.*))?$",
        routed,
    )
    if not match:
        return None
    session_id = match.group("session")
    asset = match.group("asset") or ""
    public_base = f"{_external_prefix(scope)}/api/v1/broker-sessions/{session_id}/novnc"
    return session_id, asset, public_base


@sync_to_async(thread_sensitive=True)
def _load_session(session_id):
    try:
        session = BrokerGatewaySession.objects.get(pk=session_id)
    except (BrokerGatewaySession.DoesNotExist, ValueError):
        return None
    if session.status not in {
        session.Status.STARTING,
        session.Status.WAITING_FOR_LOGIN,
        session.Status.WAITING_FOR_2FA,
        session.Status.CONNECTED,
        session.Status.DISCONNECTED,
        session.Status.LOGIN_FAILED,
        session.Status.ERROR,
    } or session.deleted_at:
        return None
    if not CONTAINER_NAME_RE.fullmatch(session.child_container_name or ""):
        return None
    return session


def _query(scope):
    return parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))


def _cookie_token(scope, session_id):
    raw = _headers(scope).get(b"cookie", b"").decode("latin-1", "replace")
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return ""
    item = cookie.get(f"broker_novnc_{str(session_id).replace('-', '')[:12]}")
    return item.value if item else ""


def _access_token(scope, session_id):
    query_token = (_query(scope).get("token") or [""])[0]
    return query_token or _cookie_token(scope, session_id)


def _is_authorized(scope, session_id):
    return validate_novnc_access_token(session_id, _access_token(scope, session_id))


async def _send_response(send, status, body=b"", headers=None):
    extra=headers or []
    values = [(b"content-length", str(len(body)).encode())]
    if not any(key.lower()==b"content-type" for key,_ in extra):values.insert(0,(b"content-type", b"text/plain; charset=utf-8"))
    values.extend(extra)
    await send({"type": "http.response.start", "status": status, "headers": values})
    await send({"type": "http.response.body", "body": body})


def _connect_page(base_path):
    authorize_url = f"{base_path}/authorize/"
    vnc_url = f"{base_path}/vnc.html"
    websocket_path = "websockify"
    vnc_query = urlencode(
        {"autoconnect": "1", "resize": "scale", "path": websocket_path}
    )
    values = json.dumps(
        {
            "authorize": authorize_url,
            "vnc": vnc_url,
            "websockify": websocket_path,
            "vncQuery": vnc_query,
        }
    )
    return f"""<!doctype html><meta charset=utf-8><title>Opening noVNC</title>
<body><p>Authorizing the private noVNC session&hellip;</p><script>
const routes={values};
(async()=>{{const p=new URLSearchParams(location.hash.slice(1));const token=p.get('access_token');
if(!token)throw new Error('Missing access token');
const r=await fetch(routes.authorize,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token}})}});
if(!r.ok)throw new Error('noVNC authorization failed');
location.replace(routes.vnc+'?'+routes.vncQuery);
}})().catch(e=>{{document.body.textContent=e.message}});</script></body>""".encode("utf-8")


async def _read_body(receive):
    body = bytearray()
    maximum = int(settings.NOVNC_PROXY_MAX_BODY_BYTES)
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise asyncio.CancelledError
        body.extend(message.get("body", b""))
        if len(body) > maximum:
            raise ValueError("Request body is too large")
        if not message.get("more_body"):
            return bytes(body)


def _cookie_header(scope, session_id, token, base_path):
    secure = _headers(scope).get(b"x-forwarded-proto", b"").lower() == b"https" or scope.get("scheme") == "https"
    name = f"broker_novnc_{str(session_id).replace('-', '')[:12]}"
    value = f"{name}={token}; Path={base_path}; Max-Age={int(settings.NOVNC_ACCESS_TOKEN_TTL_SECONDS)}; HttpOnly; SameSite=Strict"
    if secure:
        value += "; Secure"
    return value.encode("latin-1")


async def proxy_http(scope, receive, send, route):
    session_id, asset, base_path = route
    session = await _load_session(session_id)
    if session is None:
        return await _send_response(send, 404, b"Broker session not found")
    if asset.rstrip("/") == "connect":
        return await _send_response(send, 200, _connect_page(base_path), [(b"content-type", b"text/html; charset=utf-8"),
            (b"cache-control", b"no-store")])
    if asset.rstrip("/") == "authorize":
        if scope.get("method") != "POST":
            return await _send_response(send, 405, b"POST required")
        try:
            payload = json.loads(await _read_body(receive) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return await _send_response(send, 400, b"Invalid authorization request")
        token = str(payload.get("token") or "")
        if not validate_novnc_access_token(session_id, token):
            return await _send_response(send, 403, b"Invalid or expired noVNC access token")
        return await _send_response(send, 204, b"", [(b"set-cookie", _cookie_header(scope, session_id, token, base_path))])
    if not _is_authorized(scope, session_id):
        return await _send_response(send, 403, b"Invalid or expired noVNC access token")
    if asset.rstrip("/") == "websockify":
        return await _send_response(send, 426, b"WebSocket upgrade required")
    try:
        body = await _read_body(receive)
        upstream = f"http://{session.child_container_name}:8080/novnc/{asset or 'vnc.html'}"
        query = scope.get("query_string", b"").decode("latin-1")
        # The proxy access token is never forwarded to the child.
        if query:
            parsed_query=parse_qs(query)
            if {key.lower() for key in parsed_query}&{"host","hostname","upstream","scheme","port","url"}:
                return await _send_response(send,400,b"Client-supplied upstream routing is not allowed")
            safe_query = [(key, value) for key, values in parsed_query.items() if key != "token" for value in values]
            if safe_query:
                upstream += "?" + urlencode(safe_query)
        forwarded_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() in {b"accept", b"accept-encoding", b"content-type", b"if-modified-since", b"if-none-match", b"range"}
        }
        async with httpx.AsyncClient(follow_redirects=False, timeout=float(settings.NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS)) as client:
            result = await client.request(scope.get("method", "GET"), upstream, content=body, headers=forwarded_headers)
        if len(result.content) > int(settings.NOVNC_PROXY_MAX_BODY_BYTES):
            return await _send_response(send, 502, b"noVNC upstream response is too large")
        response_headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in result.headers.items()
            if key.lower().encode("latin-1") not in HOP_HEADERS and key.lower() not in {"content-length", "location"}
        ]
        return await _send_response(send, result.status_code, result.content, response_headers)
    except (httpx.HTTPError, asyncio.TimeoutError):
        return await _send_response(send, 502, b"noVNC upstream is unavailable")
    except ValueError as exc:
        return await _send_response(send, 413, str(exc).encode("utf-8"))


class _BinaryReader:
    def __init__(self, receiver):
        self.receiver = receiver
        self.buffer = bytearray()

    async def read(self, length):
        while len(self.buffer) < length:
            value = await self.receiver()
            if not isinstance(value, bytes):
                raise ValueError("RFB requires binary WebSocket frames")
            self.buffer.extend(value)
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value


def _reverse_bits(value):
    return int(f"{value:08b}"[::-1], 2)


def _vnc_auth_response(password, challenge):
    # RFB VNCAuth uses DES with the bit order of each password byte reversed.
    raw = normalize_vnc_password(password).encode("ascii")
    key = bytes(_reverse_bits(value) for value in raw)
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


async def _prepare_rfb_connection(upstream, receive, send, encrypted_password):
    """Terminate VNCAuth at the trusted proxy so the password never reaches the browser."""
    timeout = float(settings.NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS)
    upstream_iterator = upstream.__aiter__()

    async def upstream_frame():
        return await asyncio.wait_for(upstream_iterator.__anext__(), timeout=timeout)

    async def client_frame():
        while True:
            message = await asyncio.wait_for(receive(), timeout=timeout)
            if message["type"] == "websocket.disconnect":
                raise ConnectionError("noVNC client disconnected during RFB negotiation")
            if message["type"] == "websocket.receive":
                return message.get("bytes") if message.get("bytes") is not None else message.get("text")

    upstream_reader = _BinaryReader(upstream_frame)
    client_reader = _BinaryReader(client_frame)
    try:
        first_frame = await upstream_frame()
        if not isinstance(first_frame, bytes):
            raise ValueError("RFB requires binary WebSocket frames")
        if not first_frame.startswith(b"RFB "):
            await send({"type": "websocket.send", "bytes": first_frame})
            return
        upstream_reader.buffer.extend(first_frame)
        version = await upstream_reader.read(12)
    except StopAsyncIteration:
        if upstream_reader.buffer:
            await send({"type": "websocket.send", "bytes": bytes(upstream_reader.buffer)})
        return
    if not re.fullmatch(rb"RFB 003\.00[3-8]\n", version):
        await send({"type": "websocket.send", "bytes": version + bytes(upstream_reader.buffer)})
        return

    await send({"type": "websocket.send", "bytes": version})
    client_version = await client_reader.read(12)
    await upstream.send(client_version)
    minor = int(client_version[8:11])

    if minor <= 3:
        security_type = await upstream_reader.read(4)
        if struct.unpack("!I", security_type)[0] != 2:
            await send({"type": "websocket.send", "bytes": security_type})
            return
        await send({"type": "websocket.send", "bytes": struct.pack("!I", 1)})
    else:
        count = await upstream_reader.read(1)
        if not count[0]:
            reason_length = await upstream_reader.read(4)
            reason = await upstream_reader.read(struct.unpack("!I", reason_length)[0])
            await send({"type": "websocket.send", "bytes": count + reason_length + reason})
            return
        security_types = await upstream_reader.read(count[0])
        if 2 not in security_types:
            await send({"type": "websocket.send", "bytes": count + security_types})
            return
        await send({"type": "websocket.send", "bytes": b"\x01\x01"})
        if await client_reader.read(1) != b"\x01":
            raise ValueError("noVNC client rejected proxy RFB authentication")
        await upstream.send(b"\x02")

    challenge = await upstream_reader.read(16)
    password = decrypt_secret(encrypted_password)
    try:
        await upstream.send(_vnc_auth_response(password, challenge))
    finally:
        password = ""
    result = await upstream_reader.read(4)
    await send({"type": "websocket.send", "bytes": result})
    if result != b"\x00\x00\x00\x00" and minor >= 8:
        reason_length = await upstream_reader.read(4)
        reason = await upstream_reader.read(struct.unpack("!I", reason_length)[0])
        await send({"type": "websocket.send", "bytes": reason_length + reason})
        raise ValueError("Upstream VNC authentication failed")


async def proxy_websocket(scope, receive, send, route):
    session_id, asset, _ = route

    first = await receive()
    if first.get("type") != "websocket.connect":
        return

    session = await _load_session(session_id)
    if session is None:
        return await send({"type": "websocket.close", "code": 4404})

    if asset.rstrip("/") != "websockify" or not _is_authorized(scope, session_id):
        return await send({"type": "websocket.close", "code": 4403})

    # Preserve exactly what the browser offered.
    # Modern noVNC usually offers no subprotocol.
    client_subprotocols = list(scope.get("subprotocols") or [])

    upstream_url = (
        f"ws://{session.child_container_name}:8080/novnc/websockify"
    )

    try:
        async with websocket_connect(
            upstream_url,
            subprotocols=client_subprotocols or None,
            open_timeout=float(
                settings.NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS
            ),
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        ) as upstream:
            accept_message = {"type": "websocket.accept"}

            # Return a subprotocol only when the browser offered it.
            if upstream.subprotocol is not None:
                if upstream.subprotocol not in client_subprotocols:
                    raise RuntimeError(
                        "Upstream selected an unrequested WebSocket subprotocol"
                    )
                accept_message["subprotocol"] = upstream.subprotocol

            await send(accept_message)

            client_disconnected = asyncio.Event()
            encrypted_password = getattr(
                session,
                "encrypted_novnc_password",
                "",
            )

            if encrypted_password:
                await _prepare_rfb_connection(
                    upstream,
                    receive,
                    send,
                    encrypted_password,
                )

            async def client_to_upstream():
                while True:
                    try:
                        message = await asyncio.wait_for(
                            receive(),
                            timeout=float(
                                settings.NOVNC_PROXY_IDLE_TIMEOUT_SECONDS
                            ),
                        )
                    except asyncio.TimeoutError:
                        await upstream.close(code=1001)
                        return

                    if message["type"] == "websocket.disconnect":
                        client_disconnected.set()
                        await upstream.close(
                            code=message.get("code", 1000)
                        )
                        return

                    if message["type"] == "websocket.receive":
                        data = (
                            message.get("bytes")
                            if message.get("bytes") is not None
                            else message.get("text")
                        )
                        if data is not None:
                            await upstream.send(data)

            async def upstream_to_client():
                iterator = upstream.__aiter__()

                while True:
                    try:
                        data = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=float(
                                settings.NOVNC_PROXY_IDLE_TIMEOUT_SECONDS
                            ),
                        )
                    except StopAsyncIteration:
                        return
                    except asyncio.TimeoutError:
                        await upstream.close(code=1001)
                        return

                    key = "bytes" if isinstance(data, bytes) else "text"
                    await send({
                        "type": "websocket.send",
                        key: data,
                    })

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]

            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            await asyncio.gather(
                *pending,
                return_exceptions=True,
            )

            for task in done:
                task.result()

            if not client_disconnected.is_set():
                await send({
                    "type": "websocket.close",
                    "code": getattr(
                        upstream,
                        "close_code",
                        None,
                    ) or 1000,
                })

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "noVNC WebSocket proxy failed",
            extra={
                "session_id": str(session_id),
                "child_container": session.child_container_name,
                "asset": asset,
            },
        )   
        await send({
            "type": "websocket.close",
            "code": 1011,
        })


class BrokerProxyRouter:
    def __init__(self, django_application):
        self.django_application = django_application

    async def __call__(self, scope, receive, send):
        route = _route(scope)
        if route and scope["type"] == "http":
            return await proxy_http(scope, receive, send, route)
        if route and scope["type"] == "websocket":
            return await proxy_websocket(scope, receive, send, route)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4404})
        return await self.django_application(scope, receive, send)
