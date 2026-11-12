import base64
import hashlib
import hmac
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .vnc import normalize_vnc_password


class BrokerCredentialError(RuntimeError):
    pass


def _deployment_key():
    value = str(getattr(settings, "BROKER_SESSION_ENCRYPTION_KEY", "") or "").strip()
    if not value:
        raise ImproperlyConfigured("BROKER_SESSION_ENCRYPTION_KEY is required for broker sessions")
    return value.encode("utf-8")


def _fernet():
    raw = _deployment_key()
    try:
        return Fernet(raw)
    except (ValueError, UnicodeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


def encrypt_secret(value):
    if value is None or str(value) == "":
        raise BrokerCredentialError("Broker credential cannot be empty")
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    try:
        return _fernet().decrypt(str(value).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise BrokerCredentialError("Stored broker credential cannot be decrypted") from exc


def mask_username(username):
    value = str(username or "").strip()
    if len(value) <= 2:
        return "•" * len(value)
    if len(value) <= 5:
        return f"{value[0]}{'•' * (len(value) - 2)}{value[-1]}"
    return f"{value[:2]}{'•' * min(8, len(value) - 4)}{value[-2:]}"


def generate_service_token():
    return secrets.token_urlsafe(48)


def generate_novnc_password():
    return normalize_vnc_password(secrets.token_urlsafe(12))


def _novnc_signature(session_id, expires_at, nonce):
    message = f"novnc:v1:{session_id}:{int(expires_at)}:{nonce}".encode("utf-8")
    return hmac.new(hashlib.sha256(_deployment_key()).digest(), message, hashlib.sha256).digest()


def issue_novnc_access_token(session_id, ttl_seconds=None, now=None):
    ttl = int(ttl_seconds or getattr(settings, "NOVNC_ACCESS_TOKEN_TTL_SECONDS", 300))
    expires_at = int(now or time.time()) + max(30, ttl)
    nonce = secrets.token_urlsafe(18)
    signature = base64.urlsafe_b64encode(_novnc_signature(session_id, expires_at, nonce)).decode("ascii").rstrip("=")
    return f"v1.{expires_at}.{nonce}.{signature}", expires_at


def validate_novnc_access_token(session_id, token, now=None):
    try:
        version, raw_expiry, nonce, supplied = str(token or "").split(".", 3)
        expires_at = int(raw_expiry)
    except (TypeError, ValueError):
        return False
    if version != "v1" or expires_at < int(now or time.time()):
        return False
    expected = base64.urlsafe_b64encode(_novnc_signature(session_id, expires_at, nonce)).decode("ascii").rstrip("=")
    return hmac.compare_digest(supplied, expected)
