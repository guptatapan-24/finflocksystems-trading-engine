#!/usr/bin/env python3
"""Wait for Redis and prove the configured password works.

Uses the same URLs the application uses (REDIS_URL, CELERY_BROKER_URL, with REDIS_PASSWORD injected by
config/settings.py), so a wrong password fails here with a clear message instead of as an opaque Celery error.
Exits 0 when every URL answers PING, 1 otherwise. The entrypoint treats a failure as a warning and keeps starting,
so Backend diagnostics stay reachable while Redis is repaired.
"""
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import redis
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env", override=False)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def redacted(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port or 6379}{parts.path}"


def main() -> int:
    from django.conf import settings

    urls = {"REDIS_URL": settings.REDIS_URL, "CELERY_BROKER_URL": settings.CELERY_BROKER_URL}
    if not os.getenv("REDIS_PASSWORD") and not any(urlsplit(u).password for u in urls.values()):
        print("WARNING: REDIS_PASSWORD is empty; connecting to Redis without authentication.", file=sys.stderr)
    max_attempts = int(os.getenv("REDIS_BOOTSTRAP_MAX_ATTEMPTS", "30"))
    retry_seconds = float(os.getenv("REDIS_BOOTSTRAP_RETRY_SECONDS", "2"))
    failed = False
    for name, url in urls.items():
        for attempt in range(1, max_attempts + 1):
            try:
                client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
                client.ping()
                # PING is allowed for every user; a write under the key prefix proves the user can really use it.
                probe = f"{getattr(settings, 'REDIS_KEY_PREFIX', '')}startup-probe"
                client.set(probe, "1", ex=30)
                client.delete(probe)
                print(f"Redis OK: {name} -> {redacted(url)}")
                break
            except redis.AuthenticationError:
                print(f"Redis authentication failed for {name} ({redacted(url)}): check REDIS_PASSWORD.", file=sys.stderr)
                failed = True
                break  # retrying a wrong password cannot help
            except redis.ResponseError as exc:  # e.g. NOPERM: the user may not touch these keys
                print(f"Redis refused a write for {name} ({redacted(url)}): {exc}. Check the user's key permissions "
                      f"and REDIS_KEY_PREFIX.", file=sys.stderr)
                failed = True
                break
            except redis.RedisError as exc:
                print(f"Redis not ready for {name} ({redacted(url)}), attempt {attempt}/{max_attempts}: "
                      f"{type(exc).__name__}", file=sys.stderr)
                if attempt == max_attempts:
                    failed = True
                else:
                    time.sleep(retry_seconds)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
