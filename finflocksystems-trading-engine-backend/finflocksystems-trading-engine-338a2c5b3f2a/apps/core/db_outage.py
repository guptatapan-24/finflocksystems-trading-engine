"""Bounded wait-and-retry for a database outage inside a long-running worker.

A worker that lets a database connection error escape exits, and the process
supervisor restarts it straight away: every restart is a full Django boot, and
while the database is down the restarts repeat for as long as it is. Waiting in
the same process with capped exponential backoff costs almost nothing and keeps
the worker's state (for example an uncommitted Kafka message) intact.

Only database *connectivity* errors are retried. Every other exception, and a
connectivity error that outlasts ``give_up_after_seconds``, is raised unchanged,
so the caller's existing failure handling and the supervisor restart still apply.
"""

import logging
import random
import time

from django.db import connections
from django.db.utils import InterfaceError, OperationalError

logger = logging.getLogger(__name__)

DB_CONNECTION_ERRORS = (OperationalError, InterfaceError)


class DbOutageBackoff:
    def __init__(
        self,
        label,
        *,
        stop=lambda: False,
        sleep=time.sleep,
        clock=time.monotonic,
        jitter=random.random,
        base_seconds=2.0,
        cap_seconds=30.0,
        # Kept well below Kafka's default max.poll.interval.ms (300 s) so a
        # consumer waiting here is never evicted from its group.
        give_up_after_seconds=120.0,
        # An attempt that ran at least this long was healthy, so a later failure
        # starts a new outage instead of extending the previous one.
        healthy_after_seconds=60.0,
        log_every_seconds=30.0,
    ):
        self.label = label
        self._stop = stop
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self.base_seconds = base_seconds
        self.cap_seconds = cap_seconds
        self.give_up_after_seconds = give_up_after_seconds
        self.healthy_after_seconds = healthy_after_seconds
        self.log_every_seconds = log_every_seconds

    def run(self, operation):
        outage_started = None
        failures = 0
        last_logged = None
        while True:
            attempt_started = self._clock()
            try:
                result = operation()
            except DB_CONNECTION_ERRORS as exc:
                now = self._clock()
                if outage_started is None or now - attempt_started >= self.healthy_after_seconds:
                    outage_started, failures, last_logged = now, 0, None
                failures += 1
                down_for = now - outage_started
                if self._stop() or down_for >= self.give_up_after_seconds:
                    logger.error(
                        "%s: database still unavailable after %.0fs (%s); giving up",
                        self.label, down_for, type(exc).__name__,
                    )
                    raise
                delay = min(self.cap_seconds, self.base_seconds * 2 ** (failures - 1))
                delay *= 0.75 + 0.5 * self._jitter()
                delay = min(delay, max(self.give_up_after_seconds - down_for, 0.5))
                if last_logged is None or now - last_logged >= self.log_every_seconds:
                    logger.warning(
                        "%s: database unavailable (%s: %.150s); retrying in %.1fs "
                        "(failure %d, down %.0fs)",
                        self.label, type(exc).__name__, str(exc), delay, failures, down_for,
                    )
                    last_logged = now
                self._close_connections()
                self._wait(delay)
                if self._stop():
                    raise
                continue
            if outage_started is not None:
                logger.warning(
                    "%s: database available again after %.0fs", self.label,
                    self._clock() - outage_started,
                )
            return result

    def _wait(self, delay):
        deadline = self._clock() + delay
        while not self._stop():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(0.5, remaining))

    @staticmethod
    def _close_connections():
        # Force a fresh connection on the next attempt; the old one is dead.
        try:
            connections.close_all()
        except Exception:
            logger.debug("Closing database connections after an outage failed", exc_info=True)
