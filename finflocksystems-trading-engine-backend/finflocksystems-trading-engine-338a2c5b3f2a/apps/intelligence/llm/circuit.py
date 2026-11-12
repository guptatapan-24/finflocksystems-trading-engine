"""Small per-process circuit breaker. State is advisory only; it never touches trading."""
import threading
import time


class CircuitBreaker:
    def __init__(self, failure_threshold=5, cooldown_seconds=60.0, clock=time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = None

    def allow(self):
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.cooldown_seconds:
                # half-open: let one probe through; a failure re-opens immediately
                self._opened_at = None
                self._failures = self.failure_threshold - 1
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self._clock()

    @property
    def state(self):
        with self._lock:
            return "OPEN" if self._opened_at is not None else "CLOSED"


qch_breaker = CircuitBreaker()


def breaker_for(provider):
    """The breaker of the configured provider (health and metrics report this one)."""
    return qch_breaker
