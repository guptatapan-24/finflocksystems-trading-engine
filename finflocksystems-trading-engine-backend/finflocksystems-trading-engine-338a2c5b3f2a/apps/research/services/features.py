import math

import numpy as np


def _values(values):
    return np.asarray(values, dtype=float)


def sma(values, window):
    values = _values(values)
    result = np.full(len(values), np.nan)
    if window < 1:
        raise ValueError("window must be positive")
    if len(values) >= window:
        result[window - 1:] = np.convolve(values, np.ones(window) / window, mode="valid")
    return result


def ema(values, window):
    values = _values(values)
    result = np.full(len(values), np.nan)
    if window < 1:
        raise ValueError("window must be positive")
    if len(values) < window:
        return result
    result[window - 1] = np.mean(values[:window])
    alpha = 2.0 / (window + 1.0)
    for index in range(window, len(values)):
        result[index] = alpha * values[index] + (1 - alpha) * result[index - 1]
    return result


def rsi(values, window=14):
    values = _values(values)
    result = np.full(len(values), np.nan)
    if window < 2:
        raise ValueError("window must be at least two")
    if len(values) <= window:
        return result
    changes = np.diff(values)
    gains = np.maximum(changes, 0)
    losses = np.maximum(-changes, 0)
    average_gain = np.mean(gains[:window])
    average_loss = np.mean(losses[:window])
    result[window] = 100.0 if average_loss == 0 else 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    for index in range(window + 1, len(values)):
        average_gain = ((window - 1) * average_gain + gains[index - 1]) / window
        average_loss = ((window - 1) * average_loss + losses[index - 1]) / window
        result[index] = 100.0 if average_loss == 0 else 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return result


def shifted_donchian(highs, lows, window):
    """Channels through t-1, preventing current-bar breakout look-ahead."""
    highs, lows = _values(highs), _values(lows)
    if len(highs) != len(lows):
        raise ValueError("high and low lengths must match")
    upper, lower = np.full(len(highs), np.nan), np.full(len(lows), np.nan)
    for index in range(window, len(highs)):
        upper[index] = np.max(highs[index - window:index])
        lower[index] = np.min(lows[index - window:index])
    return upper, lower


def momentum(values, window):
    values = _values(values)
    result = np.full(len(values), np.nan)
    if window < 1:
        raise ValueError("window must be positive")
    result[window:] = values[window:] / values[:-window] - 1.0
    return result


def realized_volatility(values, window, annualization=252):
    values = _values(values)
    result = np.full(len(values), np.nan)
    log_returns = np.diff(np.log(values))
    for index in range(window, len(values)):
        sample = log_returns[index - window:index]
        result[index] = np.std(sample, ddof=1) * math.sqrt(annualization) if len(sample) > 1 else 0
    return result


def atr(highs, lows, closes, window=14):
    highs, lows, closes = _values(highs), _values(lows), _values(closes)
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("OHLC lengths must match")
    result = np.full(len(closes), np.nan)
    true_range = np.empty(len(closes))
    true_range[0] = highs[0] - lows[0]
    for index in range(1, len(closes)):
        true_range[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
    if len(closes) >= window:
        result[window - 1] = np.mean(true_range[:window])
        for index in range(window, len(closes)):
            result[index] = ((window - 1) * result[index - 1] + true_range[index]) / window
    return result


FEATURE_REGISTRY = {
    "sma": sma,
    "ema": ema,
    "rsi": rsi,
    "donchian_channels": shifted_donchian,
    "trailing_return": momentum,
    "realized_volatility": realized_volatility,
    "atr": atr,
}


def calculate_feature(key, *args, **kwargs):
    try:
        implementation = FEATURE_REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"Feature {key} has no tested implementation") from exc
    return implementation(*args, **kwargs)
