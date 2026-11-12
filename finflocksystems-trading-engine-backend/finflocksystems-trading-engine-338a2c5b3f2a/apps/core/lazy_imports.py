"""Deferred imports for the heavyweight numerical stack.

Celery autodiscovers a ``tasks`` module from every installed app, so a
module-scope ``import numpy`` anywhere in the reachable import graph is paid by
every worker process -- including the broker-command, intent-execution and
target-coordination workers that never evaluate a model, plus beat and both
market-data consumers.

Measured inside the backend image, against a 72.6MB bare-Django baseline:

    + numpy            +20.1MB
    + scipy.optimize   +56.6MB
    + pandas           +81.0MB

That is ~158MB of resident memory per process, and the container runs 19 of
them. A worker whose reachable graph avoids these three measured 119.5MB
instead of 250.6MB.

``lazy_module`` returns a stand-in that imports the real module on first
attribute access, so the cost lands only in the process that actually does the
arithmetic. Call sites do not change: ``np.asarray(...)`` still reads
``np.asarray(...)``.

Two constraints on modules that use this:

* The module must keep ``from __future__ import annotations``, or avoid naming
  the lazy module in a signature. A bare annotation such as ``x: np.ndarray``
  is evaluated at function-definition time and would import numpy at module
  import, defeating the deferral. It stays correct, just not lazy.
* ``from numpy import asarray``-style imports cannot be deferred this way;
  keep those inside the function that needs them.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any


class LazyModule:
    """A module stand-in that imports its target on first attribute access."""

    __slots__ = ("_lazy_name", "_lazy_module", "_lazy_lock")

    def __init__(self, name: str) -> None:
        self._lazy_name = name
        self._lazy_module: Any = None
        self._lazy_lock = threading.Lock()

    def _lazy_resolve(self) -> Any:
        module = self._lazy_module
        if module is None:
            # celery-data and celery-research run --pool=threads, so first
            # access really can be concurrent. Re-check under the lock.
            with self._lazy_lock:
                module = self._lazy_module
                if module is None:
                    module = importlib.import_module(self._lazy_name)
                    self._lazy_module = module
        return module

    def __getattr__(self, attribute: str) -> Any:
        # Guards against unbounded recursion if the instance is ever recreated
        # without __init__ having run (copy, unpickle).
        if attribute.startswith("_lazy"):
            raise AttributeError(attribute)
        return getattr(self._lazy_resolve(), attribute)

    def __dir__(self) -> list[str]:
        return dir(self._lazy_resolve())

    def __repr__(self) -> str:
        state = "loaded" if self._lazy_module is not None else "deferred"
        return f"<LazyModule {self._lazy_name!r} {state}>"


def lazy_module(name: str) -> LazyModule:
    """Return a stand-in for ``name`` that imports it on first attribute use."""
    return LazyModule(name)
