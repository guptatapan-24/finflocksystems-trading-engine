import math
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

try:
    from .identity import market_bar_id, processing_mode, raw_event_key, stable_hash
except ImportError:
    from jobs.identity import market_bar_id, processing_mode, raw_event_key, stable_hash


def utc(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def market_data_type(event):
    """Normalize latency metadata and repair legacy IBKR delayed events."""
    source = str(event.get("source") or "").strip().lower()
    if source == "ibkr_delayed":
        return "DELAYED"
    value = str(event.get("market_data_type") or "LIVE").strip().upper()
    if value not in {"LIVE", "DELAYED"}:
        raise ValueError(f"unsupported market_data_type {value}")
    return value


def normalize_market_event(raw, symbol_map, mode_override=None):
    kind=raw.get("event_kind","TICK").upper()
    required = ["source_event_id", "symbol", "event_time"] + (["open","high","low","close","timeframe","window_start","window_end"] if kind=="BAR" else ["price"])
    missing = [key for key in required if raw.get(key) in (None, "")]
    if missing:
        raise ValueError(f"missing fields: {','.join(missing)}")
    instrument_id = raw.get("instrument_id") or symbol_map.get(str(raw.get("conid"))) or symbol_map.get(raw["symbol"])
    if instrument_id is None:
        raise ValueError("unknown instrument mapping")
    price, volume = Decimal(str(raw.get("price",raw.get("close")))), Decimal(str(raw.get("volume", 0)))
    if price <= 0 or volume < 0:
        raise ValueError("price must be positive and volume non-negative")
    result={"source_event_id": str(raw["source_event_id"]), "instrument_id": str(instrument_id),"conid":raw.get("conid"),
        "symbol": raw["symbol"], "event_time": utc(raw["event_time"]).isoformat(),
        "exchange": raw.get("exchange", "SMART"), "currency": raw.get("currency", "USD"),
        "price": str(price), "volume": str(volume), "source": raw.get("source", "ibkr"),"event_kind":kind,
        "provider":raw.get("provider","IBKR"),"provider_symbol":raw.get("provider_symbol"),
        "provider_generation":raw.get("provider_generation"),"subscription_key":raw.get("subscription_key"),
        "market_data_type":market_data_type(raw),
        "received_at":raw.get("received_at"),
        "fallback_reason":raw.get("fallback_reason",""),
        "processing_mode":processing_mode(mode_override or raw.get("processing_mode"))}
    if kind=="BAR":
        values={key:Decimal(str(raw[key])) for key in ("open","high","low","close")}
        if min(values.values())<=0 or values["low"]>values["high"]:raise ValueError("invalid OHLC values")
        result.update({key:str(value) for key,value in values.items()})
        result.update({"timeframe":raw["timeframe"],"window_start":utc(raw["window_start"]).isoformat(),
            "window_end":utc(raw["window_end"]).isoformat(),"is_final":bool(raw.get("is_final",True))})
    return result


def window_start(event_time, seconds):
    stamp = int(utc(event_time).timestamp())
    return datetime.fromtimestamp(stamp - stamp % seconds, tz=timezone.utc)


def aggregate_bars(events, interval="1m", seconds=60, prior_versions=None, final=True):
    grouped = defaultdict(list)
    for event in events:
        grouped[(
            str(event["instrument_id"]),
            window_start(event["event_time"], seconds),
            processing_mode(event.get("processing_mode")),
        )].append(event)
    result = []
    prior_versions = prior_versions or {}
    for (instrument_id, start, mode), ticks in sorted(grouped.items()):
        ticks.sort(key=lambda x: (utc(x["event_time"]), x["source_event_id"]))
        opens=[Decimal(str(x.get("open",x["price"]))) for x in ticks]
        highs=[Decimal(str(x.get("high",x["price"]))) for x in ticks]
        lows=[Decimal(str(x.get("low",x["price"]))) for x in ticks]
        closes=[Decimal(str(x.get("close",x["price"]))) for x in ticks]
        bar_id = market_bar_id(instrument_id,interval,start.isoformat())
        causal_tick=ticks[-1]
        result.append({"bar_id": bar_id, "instrument_id": instrument_id, "interval": interval,
            "window_start": start.isoformat(), "window_end": datetime.fromtimestamp(start.timestamp()+seconds, tz=timezone.utc).isoformat(),
            "open": str(opens[0]), "high": str(max(highs)), "low": str(min(lows)), "close": str(closes[-1]),
            "volume": str(sum(Decimal(str(x.get("volume", 0))) for x in ticks)),
            "source_event_count": len(ticks), "version": prior_versions.get(bar_id, 0) + 1,
            "provider":causal_tick.get("provider",""),
            "provider_generation":causal_tick.get("provider_generation") or "",
            "source":causal_tick.get("source",""),
            "market_data_type":causal_tick.get("market_data_type","LIVE"),
            "received_at":causal_tick.get("received_at"),
            "is_final": final, "processing_mode":mode})
    return result


def bar_content_fingerprint(bar):
    return stable_hash({
        key: bar.get(key, "")
        for key in (
            "bar_id", "instrument_id", "interval", "window_start", "window_end",
            "open", "high", "low", "close", "volume", "source_event_count",
            "provider", "provider_generation", "source",
            "market_data_type",
        )
    })


def advance_bar_version(bucket, bar):
    """Return checkpointable version state and a new version only for new content."""
    updated = dict(bucket)
    fingerprint = bar_content_fingerprint(bar)
    if fingerprint == updated.get("fingerprint"):
        return updated, None
    updated["fingerprint"] = fingerprint
    updated["version"] = int(updated.get("version", 0)) + 1
    return updated, updated["version"]


# ---------------------------------------------------------------------------
# Bounded Flink state, rebuilt from PostgreSQL through the transactional outbox
# ---------------------------------------------------------------------------
#
# Flink keyed state is a working cache, not the system of record: PostgreSQL
# owns the requirement registry (StrategyInputRequirement/Binding) and the bar
# history (MarketBar). The backend re-feeds both through the outbox
# (apps/market_streams/rehydration.py), so the helpers below can keep Flink
# state small and still recover after it is lost.

STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE = "strategy.inputs.snapshot"
BAR_HISTORY_SEED_EVENT_TYPE = "market.bar.seed"
REGISTRY_META_PREFIX = "meta:"
HISTORY_BAR_FIELDS = (
    "bar_id", "instrument_id", "interval", "version", "window_start", "window_end",
    "open", "high", "low", "close", "volume", "is_final",
)


def is_registry_meta_key(key):
    return str(key).startswith(REGISTRY_META_PREFIX)


def registry_meta_key(instrument_id, timeframe):
    return f"{REGISTRY_META_PREFIX}{instrument_id}:{timeframe}"


def _registry_item(requirement, instrument_id, timeframe):
    return {**requirement, "instrument_id": str(instrument_id), "timeframe": timeframe}


def plan_registry_update(event, current, input_type):
    """Return ``(puts, removes)`` for one ``strategy.inputs.v1`` event.

    ``current`` maps broadcast-state keys to decoded items, including the
    ``meta:<instrument>:<timeframe>`` bookkeeping entries. Incremental
    ``strategy.inputs.changed`` events keep their historical upsert/remove
    behaviour. A ``strategy.inputs.snapshot`` published from PostgreSQL
    *replaces* every requirement of this stage's input type for its
    (instrument, timeframe), unless an incremental event newer than the
    snapshot was already applied: a snapshot that was retried late must not
    undo a newer activation or deactivation.
    """
    payload = event.get("payload") or {}
    instrument_id = str(payload.get("instrument_id"))
    timeframe = payload.get("timeframe")
    meta_key = registry_meta_key(instrument_id, timeframe)
    occurred_at = event.get("occurred_at")
    last_applied = (current.get(meta_key) or {}).get("occurred_at")
    wanted = {
        requirement["identity_hash"]: _registry_item(requirement, instrument_id, timeframe)
        for requirement in payload.get("requirements", [])
        if requirement.get("input_type") == input_type
    }
    puts, removes = {}, []
    if event.get("event_type") == STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE:
        if last_applied and occurred_at and utc(occurred_at) < utc(last_applied):
            return {}, []
        removes = [
            key for key, item in current.items()
            if not is_registry_meta_key(key)
            and str(item.get("instrument_id")) == instrument_id
            and item.get("timeframe") == timeframe
            and key not in wanted
        ]
    else:
        removes = list(payload.get("removed_requirement_hashes", []))
    puts.update(wanted)
    if occurred_at and (not last_applied or utc(occurred_at) > utc(last_applied)):
        puts[meta_key] = {"occurred_at": occurred_at}
    return puts, removes


def apply_registry_event(state, event, input_type):
    """Apply :func:`plan_registry_update` to a broadcast ``MapState`` of JSON strings."""
    import json

    current = {key: json.loads(value) for key, value in state.items()}
    puts, removes = plan_registry_update(event, current, input_type)
    for key in removes:
        state.remove(key)
    for key, item in puts.items():
        state.put(key, json.dumps(item, separators=(",", ":")))
    return puts, removes


def compact_history_bar(bar):
    """Keep only the fields indicator computation and ordering read."""
    return {key: bar[key] for key in HISTORY_BAR_FIELDS if key in bar}


def history_limit(requirements, default_max_bars):
    """Bars to retain for one (instrument, timeframe): the configured cap, but
    never fewer than the longest warm-up a registered requirement declares."""
    needed = max((int(item.get("warmup_bars") or 0) for item in requirements), default=0) + 1
    return max(int(default_max_bars), needed)


def _history_order(bar):
    return (utc(bar["window_end"]), int(bar.get("version", 1)))


def merge_bar_history(history, incoming, max_bars):
    """Union two bar lists by ``bar_id`` keeping the highest version.

    Order-independent, so a seed replayed from Kafka before or after live bars
    yields the same history.
    """
    by_id = {}
    for bar in [*history, *incoming]:
        if not bar.get("is_final", True):
            continue
        compact = compact_history_bar(bar)
        previous = by_id.get(compact["bar_id"])
        if previous is None or int(compact.get("version", 1)) >= int(previous.get("version", 1)):
            by_id[compact["bar_id"]] = compact
    ordered = sorted(by_id.values(), key=_history_order)
    return ordered[-int(max_bars):] if int(max_bars) > 0 else ordered


def buffer_unknown_registry_event(pending, incoming, expires_at_ms, maximum_events):
    """Return a bounded, source-id deduplicated pending list and any capacity eviction."""
    payload = incoming.get("payload", incoming)
    identity = raw_event_key(payload)
    if any(item["identity"] == identity for item in pending):
        return list(pending), []
    updated = [*pending, {
        "identity": identity,
        "expires_at_ms": int(expires_at_ms),
        "event": incoming,
    }]
    updated.sort(key=lambda item: (item["expires_at_ms"], item["identity"]))
    overflow = updated[:-maximum_events] if len(updated) > maximum_events else []
    return updated[-maximum_events:], overflow


def partition_expired_registry_events(pending, now_ms):
    expired = [item for item in pending if item["expires_at_ms"] <= int(now_ms)]
    remaining = [item for item in pending if item["expires_at_ms"] > int(now_ms)]
    return remaining, expired


def _sma(values, n):
    return sum(values[-n:]) / Decimal(n) if len(values) >= n else None


def _rsi(values, n):
    if len(values) <= n:
        return None
    changes = [values[i] - values[i-1] for i in range(len(values)-n, len(values))]
    gains = sum(max(x, Decimal(0)) for x in changes) / Decimal(n)
    losses = sum(max(-x, Decimal(0)) for x in changes) / Decimal(n)
    return Decimal(100) if losses == 0 else Decimal(100) - Decimal(100) / (Decimal(1) + gains/losses)


def _completed_weekly_bars(ordered):
    """Aggregate daily bars into completed ISO weeks only.

    The current bar's week is deliberately excluded. This preserves the RPS V2
    causal boundary for the whole trading week and prevents a partial week from
    moving the entry range.
    """
    if not ordered:
        return []
    current_week = utc(ordered[-1]["window_end"]).date().isocalendar()[:2]
    grouped = {}
    for bar in ordered:
        stamp = utc(bar["window_end"])
        key = stamp.date().isocalendar()[:2]
        if key == current_week:
            continue
        values = {name: Decimal(str(bar[name])) for name in ("open", "high", "low", "close")}
        if key not in grouped:
            grouped[key] = {
                "open": values["open"], "high": values["high"],
                "low": values["low"], "close": values["close"],
            }
        else:
            grouped[key]["high"] = max(grouped[key]["high"], values["high"])
            grouped[key]["low"] = min(grouped[key]["low"], values["low"])
            grouped[key]["close"] = values["close"]
    return [grouped[key] for key in sorted(grouped)]


def _wilder_atr(weekly, period):
    if len(weekly) < period:
        return None
    true_ranges = []
    previous_close = None
    for bar in weekly:
        candidates = [bar["high"] - bar["low"]]
        if previous_close is not None:
            candidates.extend((abs(bar["high"] - previous_close), abs(bar["low"] - previous_close)))
        true_ranges.append(max(candidates))
        previous_close = bar["close"]
    # Equivalent to pandas ewm(alpha=1/period, adjust=False,
    # min_periods=period), as used by Aryan_Options.
    atr = true_ranges[0]
    alpha = Decimal(1) / Decimal(period)
    for value in true_ranges[1:]:
        atr = alpha * value + (Decimal(1) - alpha) * atr
    return atr


def compute_indicators(bars, fast=20, slow=50, rsi_period=14, donchian=20, momentum_period=20, volatility_period=20, adv_period=20):
    ordered = sorted((x for x in bars if x.get("is_final", True)), key=lambda x: utc(x["window_end"]))
    closes = [Decimal(x["close"]) for x in ordered]
    highs = [Decimal(x["high"]) for x in ordered]
    lows = [Decimal(x["low"]) for x in ordered]
    volumes = [Decimal(x.get("volume", 0)) for x in ordered]
    if not ordered:
        return {}
    returns = [(closes[i]/closes[i-1])-1 for i in range(1, len(closes)) if closes[i-1] != 0]
    recent_returns = returns[-volatility_period:]
    vol = None
    if len(recent_returns) >= 2:
        mean = sum(recent_returns)/Decimal(len(recent_returns))
        variance = sum((x-mean)**2 for x in recent_returns)/Decimal(len(recent_returns)-1)
        vol = Decimal(str(math.sqrt(float(variance)))) * Decimal(str(math.sqrt(252)))
    return {"sma_fast": _sma(closes, fast), "sma_slow": _sma(closes, slow), "rsi": _rsi(closes, rsi_period),
        "donchian_upper": max(highs[-donchian:]) if len(highs) >= donchian else None,
        "donchian_lower": min(lows[-donchian:]) if len(lows) >= donchian else None,
        "momentum": closes[-1]/closes[-momentum_period-1]-1 if len(closes) > momentum_period and closes[-momentum_period-1] else None,
        "realized_volatility": vol, "average_volume": _sma(volumes, adv_period), "reference_price": closes[-1]}


def compute_indicator(bars, name, parameters):
    """Compute one registry-declared indicator identity, independently of a strategy."""
    ordered = sorted((x for x in bars if x.get("is_final", True)), key=lambda x: utc(x["window_end"]))
    if not ordered:
        return None
    closes=[Decimal(str(x["close"])) for x in ordered];highs=[Decimal(str(x["high"])) for x in ordered]
    lows=[Decimal(str(x["low"])) for x in ordered];volumes=[Decimal(str(x.get("volume",0))) for x in ordered]
    window=int(parameters.get("window",20))
    if name=="rsi":return _rsi(closes,window)
    if name=="sma":return _sma(closes,window)
    if name=="donchian":
        use_upper=parameters.get("side",parameters.get("role","entry")) in {"upper","entry"}
        prior=(highs if use_upper else lows)[:-1]
        if len(prior)<window:return None
        return max(prior[-window:]) if use_upper else min(prior[-window:])
    if name=="momentum":return closes[-1]/closes[-window-1]-1 if len(closes)>window and closes[-window-1] else None
    if name=="realized_volatility":
        returns=[closes[i]/closes[i-1]-1 for i in range(max(1,len(closes)-window),len(closes)) if closes[i-1]]
        if len(returns)<2:return None
        mean=sum(returns)/Decimal(len(returns));variance=sum((x-mean)**2 for x in returns)/Decimal(len(returns)-1)
        return Decimal(str(math.sqrt(float(variance))))*Decimal(str(math.sqrt(252)))
    if name=="average_volume":return _sma(volumes,window)
    if name in {"weekly_atr", "weekly_close"}:
        weekly = _completed_weekly_bars(ordered)
        if not weekly:return None
        if name == "weekly_close":return weekly[-1]["close"]
        return _wilder_atr(weekly, window)
    if name=="reference_price":return closes[-1]
    raise ValueError(f"unsupported indicator: {name}")


def quality_state(latest_event_at, now, stale_after_seconds):
    if latest_event_at is None:
        return "UNAVAILABLE"
    return "STALE" if (utc(now)-utc(latest_event_at)).total_seconds() > stale_after_seconds else "FRESH"
