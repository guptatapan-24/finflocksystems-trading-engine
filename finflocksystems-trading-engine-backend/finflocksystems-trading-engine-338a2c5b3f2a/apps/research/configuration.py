from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _boolean(environment: Mapping[str, str], key: str, default: bool) -> bool:
    value = str(environment.get(key, "true" if default else "false")).strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"{key} must be true or false")
    return value == "true"


def _integer(environment: Mapping[str, str], key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(str(environment.get(key, default)).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{key} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class RecommendationSystemConfiguration:
    research_enabled: bool
    recommendation_system_enabled: bool
    universe_key: str
    maximum_stocks: int
    minimum_stocks: int
    candidate_pool_size: int
    maximum_strategies_per_stock: int
    daily_lookback_years: int
    intraday_lookback_days: int
    minimum_daily_bars: int
    score_max_age_days: int
    stale_score_fallback_days: int
    snapshot_max_age_hours: int
    maximum_parallel_data_tasks: int
    maximum_parallel_backtest_tasks: int
    artifact_root: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, default_artifact_root: Path) -> "RecommendationSystemConfiguration":
        configuration = cls(
            research_enabled=_boolean(environment, "RESEARCH_ENABLED", True),
            recommendation_system_enabled=_boolean(environment, "RECOMMENDATION_SYSTEM_ENABLED", True),
            universe_key=str(environment.get("RECOMMENDATION_UNIVERSE_KEY", "US_LARGE_CAP_GICS")).strip(),
            maximum_stocks=_integer(environment, "RECOMMENDATION_MAX_STOCKS", 20),
            minimum_stocks=_integer(environment, "RECOMMENDATION_MIN_STOCKS", 5),
            candidate_pool_size=_integer(environment, "RECOMMENDATION_CANDIDATE_POOL_SIZE", 100),
            maximum_strategies_per_stock=_integer(environment, "RECOMMENDATION_MAX_STRATEGIES_PER_STOCK", 1),
            daily_lookback_years=_integer(environment, "RESEARCH_DAILY_LOOKBACK_YEARS", 10),
            intraday_lookback_days=_integer(environment, "RESEARCH_INTRADAY_LOOKBACK_DAYS", 90),
            minimum_daily_bars=_integer(environment, "RESEARCH_MINIMUM_DAILY_BARS", 756),
            score_max_age_days=_integer(environment, "RESEARCH_SCORE_MAX_AGE_DAYS", 7),
            stale_score_fallback_days=_integer(environment, "RESEARCH_STALE_SCORE_FALLBACK_DAYS", 30),
            snapshot_max_age_hours=_integer(environment, "RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS", 24),
            maximum_parallel_data_tasks=_integer(environment, "RESEARCH_MAX_PARALLEL_DATA_TASKS", 8),
            maximum_parallel_backtest_tasks=_integer(environment, "RESEARCH_MAX_PARALLEL_BACKTEST_TASKS", 8),
            artifact_root=Path(environment.get("RESEARCH_ARTIFACT_ROOT", default_artifact_root)).expanduser(),
        )
        configuration.validate()
        return configuration

    def validate(self) -> None:
        if not self.universe_key:
            raise RuntimeError("RECOMMENDATION_UNIVERSE_KEY cannot be empty")
        if self.minimum_stocks > self.maximum_stocks:
            raise RuntimeError("RECOMMENDATION_MIN_STOCKS cannot exceed RECOMMENDATION_MAX_STOCKS")
        if self.candidate_pool_size < self.maximum_stocks:
            raise RuntimeError("RECOMMENDATION_CANDIDATE_POOL_SIZE cannot be below RECOMMENDATION_MAX_STOCKS")
        if self.maximum_strategies_per_stock != 1:
            raise RuntimeError("The long-only Portfolio Builder requires exactly one primary strategy per stock")
        if self.minimum_daily_bars < 756:
            raise RuntimeError("RESEARCH_MINIMUM_DAILY_BARS cannot be below 756")
        if self.daily_lookback_years < 3:
            raise RuntimeError("RESEARCH_DAILY_LOOKBACK_YEARS must be at least 3")
        if self.stale_score_fallback_days < self.score_max_age_days:
            raise RuntimeError("RESEARCH_STALE_SCORE_FALLBACK_DAYS cannot be below RESEARCH_SCORE_MAX_AGE_DAYS")

