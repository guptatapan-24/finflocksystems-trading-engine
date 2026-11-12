"""apply_execution before/after (CLAUDE.md Phase 2): the only addition on the hot path is the post_save handler,
which registers one after-commit callback. Run with -s to see the numbers. The assertion is deliberately loose
(a regression to 'does real work in the handler' would be several times slower, not a few percent)."""
import time

import pytest

from tests.intelligence import helpers as h

pytestmark = pytest.mark.django_db
ROUNDS = 60


def _run(settings, enabled, pf, inst, strat, tag):
    settings.INTELLIGENCE_ENABLED = enabled
    started = time.perf_counter()
    for i in range(ROUNDS):
        h.fill(pf, inst, "BUY" if i % 2 == 0 else "SELL", 1, "10", f"{tag}-{i}", instance=strat, when=h.at(3, hours=i))
    return (time.perf_counter() - started) / ROUNDS * 1000


def test_apply_execution_overhead_is_negligible(settings, monkeypatch):
    from apps.intelligence import tasks

    monkeypatch.setattr(tasks.process_fill, "apply_async", lambda *a, **k: None)
    pf = h.portfolio("USD", "DU-BENCH")
    inst = h.aapl()
    strat = h.stock_strategy(pf, inst)
    _run(settings, False, pf, inst, strat, "warm")
    off = min(_run(settings, False, pf, inst, strat, f"off{n}") for n in range(2))
    on = min(_run(settings, True, pf, inst, strat, f"on{n}") for n in range(2))
    print(f"\napply_execution mean ms/fill: disabled={off:.2f} enabled={on:.2f} overhead={(on / off - 1) * 100:+.1f}%")
    assert on < off * 1.5
