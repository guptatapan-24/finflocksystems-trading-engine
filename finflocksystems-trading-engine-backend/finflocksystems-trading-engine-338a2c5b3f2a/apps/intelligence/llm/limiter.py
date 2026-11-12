"""Concurrency limiter shared by every process that calls the LLM gateway (R22).

Redis holds a sorted set of lease expiry times, so a crashed worker cannot leak a slot for longer than the lease.
If Redis is unreachable the limiter degrades to a per-process semaphore: intelligence fails open (R2), it never blocks
or raises into trading code.
"""
import threading
import time
import uuid
from contextlib import contextmanager

from django.conf import settings

from .base import LlmError

# KEYS[1]=set, ARGV: now, lease_seconds, max, token. Returns 1 when the lease was taken.
_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[4])
  redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])) + 5)
  return 1
end
return 0
"""


class LlmBusy(LlmError):
    """No slot became free within the wait budget; the caller falls back (R2)."""


class ConcurrencyLimiter:
    def __init__(self, name, max_concurrency=None, lease_seconds=None, redis_client=None, sleep=time.sleep,
                 clock=time.monotonic):
        self.key = f"{getattr(settings, 'REDIS_KEY_PREFIX', '')}intelligence:llm-slots:{name}"
        self.max = max(1, int(max_concurrency or getattr(settings, "INTELLIGENCE_LLM_MAX_CONCURRENCY", 3)))
        self.lease = float(lease_seconds or float(getattr(settings, "INTELLIGENCE_LLM_TIMEOUT_SECONDS", 60)) * 2 + 30)
        self._redis = redis_client
        self._redis_failed = False
        self._local = threading.BoundedSemaphore(self.max)
        self._sleep = sleep
        self._clock = clock

    def _client(self):
        if self._redis_failed:
            return None
        if self._redis is None:
            try:
                import redis

                self._redis = redis.Redis.from_url(getattr(settings, "REDIS_URL", "redis://localhost:6379/0"),
                                                   socket_connect_timeout=1, socket_timeout=1)
            except Exception:
                self._redis_failed = True
                return None
        return self._redis

    def _try_redis(self, token):
        client = self._client()
        if client is None:
            return None
        try:
            return bool(client.eval(_ACQUIRE, 1, self.key, time.time(), self.lease, self.max, token))
        except Exception:
            self._redis_failed = True  # stay local for this process; a restart retries Redis
            return None

    @contextmanager
    def slot(self, wait_seconds=30.0):
        token = uuid.uuid4().hex
        deadline = self._clock() + wait_seconds
        via_redis = False
        while True:
            taken = self._try_redis(token)
            if taken is None:  # Redis unavailable: per-process limit
                if not self._local.acquire(timeout=max(0.0, deadline - self._clock())):
                    raise LlmBusy("LLM concurrency limit reached")
                break
            if taken:
                via_redis = True
                break
            if self._clock() >= deadline:
                raise LlmBusy("LLM concurrency limit reached")
            self._sleep(0.2)
        try:
            yield
        finally:
            if via_redis:
                try:
                    self._client().zrem(self.key, token)
                except Exception:
                    pass  # the lease expires on its own
            else:
                self._local.release()
