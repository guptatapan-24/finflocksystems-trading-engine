from __future__ import annotations

import numpy as np


def columns(bars):
    return {
        key: np.asarray([float(item.get(key, 0)) for item in bars], dtype=float)
        for key in ("open", "high", "low", "close", "volume")
    }


def bounded_long(values):
    return np.clip(np.nan_to_num(np.asarray(values, dtype=float), nan=0.0), 0.0, 1.0).tolist()


def rolling_mean(values, window):
    values = np.asarray(values, dtype=float)
    output = np.full(len(values), np.nan)
    if window < 1:
        raise ValueError("window must be positive")
    for index in range(window - 1, len(values)):
        output[index] = np.mean(values[index - window + 1:index + 1])
    return output


def rolling_std(values, window):
    values = np.asarray(values, dtype=float)
    output = np.full(len(values), np.nan)
    if window < 2:
        raise ValueError("window must be at least two")
    for index in range(window - 1, len(values)):
        output[index] = np.std(values[index - window + 1:index + 1], ddof=1)
    return output


def parameter(parameters, name, default, cast=float):
    value = parameters.get(name, default)
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} has an invalid value") from exc

