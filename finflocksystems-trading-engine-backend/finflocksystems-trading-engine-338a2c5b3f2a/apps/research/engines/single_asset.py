from dataclasses import dataclass

import numpy as np

from .base import ResearchProtocolContext, SingleAssetResearchStrategy


@dataclass(frozen=True)
class BacktestResult:
    returns: list[float]
    equity: list[float]
    positions: list[float]
    trades: list[dict]
    metrics: dict
    diagnostics: dict


def performance_metrics(returns, positions=None, annualization=252):
    returns = np.asarray(returns, dtype=float)
    positions = np.asarray(positions if positions is not None else np.zeros(len(returns)), dtype=float)
    if len(returns) == 0:
        return {"total_return":0.0,"cagr":0.0,"annualized_volatility":0.0,"sharpe":0.0,
                "sortino":0.0,"calmar":0.0,"max_drawdown":0.0,"drawdown_duration":0,
                "turnover":0.0,"trade_count":0,"exposure":0.0,"win_rate":0.0,"hit_rate":0.0,
                "profit_factor":0.0}
    equity = np.cumprod(1.0 + returns)
    years = max(len(returns) / annualization, 1.0 / annualization)
    cagr = float(equity[-1] ** (1.0 / years) - 1.0) if equity[-1] > 0 else -1.0
    volatility = float(np.std(returns, ddof=1) * np.sqrt(annualization)) if len(returns) > 1 else 0.0
    sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(annualization)) if len(returns) > 1 and np.std(returns, ddof=1) > 0 else 0.0
    downside=returns[returns<0]
    downside_deviation=float(np.std(downside,ddof=1)) if len(downside)>1 else 0.0
    sortino=float(np.mean(returns)/downside_deviation*np.sqrt(annualization)) if downside_deviation>0 else 0.0
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity / peaks - 1.0
    maximum_drawdown = abs(float(np.min(drawdowns)))
    turnover = float(np.sum(np.abs(np.diff(np.r_[0.0, positions]))))
    duration=longest=current=0
    for value in drawdowns:
        current=current+1 if value<0 else 0
        longest=max(longest,current)
    nonzero=returns[np.abs(returns)>1e-15]
    wins=nonzero[nonzero>0];losses=nonzero[nonzero<0]
    win_rate=float(len(wins)/len(nonzero)) if len(nonzero) else 0.0
    gross_profit=float(np.sum(wins));gross_loss=abs(float(np.sum(losses)))
    return {
        "total_return": float(equity[-1] - 1.0),
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "sortino":sortino,
        "calmar": cagr / maximum_drawdown if maximum_drawdown > 0 else 0.0,
        "max_drawdown": maximum_drawdown,
        "maximum_drawdown":maximum_drawdown,
        "drawdown_duration":longest,
        "turnover": turnover,
        "trade_count": int(np.count_nonzero(np.abs(np.diff(np.r_[0.0, positions])) > 1e-12)),
        "exposure": float(np.mean(np.abs(positions))),
        "win_rate":win_rate,
        "hit_rate":win_rate,
        "profit_factor":gross_profit/gross_loss if gross_loss>0 else (999.0 if gross_profit>0 else 0.0),
    }


class SingleAssetBacktestEngine:
    def run(self, strategy: SingleAssetResearchStrategy, bars: list[dict], parameters=None, context=None):
        context = context or ResearchProtocolContext()
        parameters = parameters or {}
        if not context.next_bar_execution:
            raise ValueError("Research execution must preserve next-bar execution")
        if len(bars) < 2:
            raise ValueError("At least two bars are required")
        opens = np.asarray([float(item["open"]) for item in bars], dtype=float)
        volumes = np.asarray([float(item.get("volume", 0)) for item in bars], dtype=float)
        if np.any(opens <= 0):
            raise ValueError("Open prices must be positive")
        signal_result = strategy.signals(bars, parameters, context)
        desired = np.asarray(signal_result.desired_exposure, dtype=float)
        if len(desired) != len(bars):
            raise ValueError("Desired exposure must have one value per bar")
        desired = np.nan_to_num(desired, nan=0.0)
        if context.long_only and np.any(desired < -1e-12):
            raise ValueError("Long-only research cannot emit negative exposure")
        if np.any(np.abs(desired) > 1.0 + 1e-12):
            raise ValueError("Exposure must remain within [-1, 1]")
        positions = np.zeros(len(bars))
        positions[1:] = desired[:-1]
        asset_returns = np.zeros(len(bars))
        asset_returns[1:] = opens[1:] / opens[:-1] - 1.0
        gross = positions * asset_returns
        trades = []
        costs = np.zeros(len(bars))
        previous = 0.0
        for index in range(1, len(bars)):
            change = positions[index] - previous
            if abs(change) > 1e-12:
                dollar_volume = max(opens[index] * volumes[index], 1.0)
                participation = min(abs(change) / dollar_volume, context.maximum_participation)
                bps = (
                    context.commission_bps
                    + context.spread_bps / 2.0
                    + context.cost_stress_bps
                    + context.impact_coefficient * np.sqrt(participation) * 10_000.0
                )
                costs[index] = abs(change) * bps / 10_000.0
                trades.append({
                    "bar_index": index,
                    "signal_bar_index": index - 1,
                    "target_exposure": float(positions[index]),
                    "change": float(change),
                    "fill_price": float(opens[index]),
                    "cost": float(costs[index]),
                })
            previous = positions[index]
        net = gross - costs
        equity = np.cumprod(1.0 + net)
        return BacktestResult(
            returns=net.tolist(),
            equity=equity.tolist(),
            positions=positions.tolist(),
            trades=trades,
            metrics={**performance_metrics(net, positions, context.annualization),
                     "total_cost":float(np.sum(costs)),"average_cost_per_trade":float(np.mean(costs[costs>0])) if np.any(costs>0) else 0.0},
            diagnostics={**signal_result.diagnostics, "total_cost": float(np.sum(costs)), "execution": "NEXT_OPEN"},
        )
