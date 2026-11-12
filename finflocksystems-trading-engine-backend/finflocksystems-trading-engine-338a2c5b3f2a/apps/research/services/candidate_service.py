from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.portfolio_construction.models import StrategyConstructionProfile
from apps.portfolio_construction.rules import resolved_goal_rules

from ..enums import ImplementationStatus
from ..models import ResearchCandidateScore, ResearchStrategyReadiness, ResearchTrial
from .scoring import candidate_score


def _clamp(value):return max(0.0,min(100.0,float(value)))


def _components(trial):
    summary=trial.summary_metrics or {};validation=trial.validation_metrics or {}
    return {
        "out_of_sample_sharpe":_clamp(50+20*float(summary.get("sharpe",0))),
        "calmar":_clamp(50+15*float(summary.get("calmar",0))),
        "drawdown_fit":_clamp(100*(1-float(summary.get("max_drawdown",1))/max(float(validation.get("maximum_allowed_drawdown",.40)),1e-9))),
        "regime_consistency":_clamp(validation.get("regime_consistency_score",0)),
        "parameter_stability":_clamp(validation.get("parameter_stability_score",0)),
        "cost_resilience":_clamp(validation.get("cost_resilience_score",0)),
        "turnover_efficiency":_clamp(validation.get("turnover_efficiency_score",0)),
        "capacity":_clamp(validation.get("capacity_score",0)),
        "diversification_contribution":_clamp(validation.get("diversification_score",0)),
    }


def _hard_metrics(trial):
    validation=trial.validation_metrics or {};summary=trial.summary_metrics or {}
    return {
        "data_quality_pass":validation.get("data_quality_pass",False),
        "timestamps_unambiguous":validation.get("timestamps_unambiguous",False),
        "high_cost_net_return":validation.get("high_cost_net_return",-1),
        "maximum_drawdown":summary.get("max_drawdown",1),
        "maximum_allowed_drawdown":validation.get("maximum_allowed_drawdown",.40),
        "trade_count":summary.get("trade_count",0),"minimum_trades":validation.get("minimum_trades",1),
        "activity_threshold_pass":validation.get("strategy_activity_pass",False),
        "parameter_neighborhood_stable":validation.get("parameter_neighborhood_stable",False),
        "capacity_pass":validation.get("capacity_pass",False),
        "largest_subperiod_contribution":validation.get("largest_subperiod_contribution",1),
        "maximum_subperiod_contribution":validation.get("maximum_subperiod_contribution",.60),
        "multiple_testing_pass":validation.get("multiple_testing_pass",False),
        "holdout_untouched":validation.get("holdout_untouched",False),
    }


def _compatible_pairs(strategy):
    for timeframe in strategy.recommended_goal_timeframes:
        if timeframe=="NOW":continue
        for risk_level in strategy.recommended_risk_levels:
            try:resolved_goal_rules(timeframe,risk_level)
            except ValueError:continue
            yield timeframe,int(risk_level)


def _update_lifecycle(strategy,has_score,has_eligible):
    implementation=strategy.implementations.filter(exact_semantic_match=True).select_related(
        "executable_strategy_definition"
    ).order_by("-updated_at").first()
    if not implementation:return
    if has_score and implementation.status in {ImplementationStatus.VALIDATED,ImplementationStatus.BACKTESTED}:
        implementation.status=ImplementationStatus.SCORED
    if has_eligible and implementation.status in {ImplementationStatus.SCORED,ImplementationStatus.BACKTESTED}:
        implementation.status=ImplementationStatus.APPROVED_FOR_RECOMMENDATION
    evidence=implementation.approval_record or {}
    if has_eligible and evidence.get("paper_validated") is True:
        implementation.status=ImplementationStatus.PAPER_VALIDATED
        profile=StrategyConstructionProfile.objects.filter(
            strategy_definition=implementation.executable_strategy_definition,construction_enabled=True
        ).exists()
        if profile:implementation.status=ImplementationStatus.BUILDER_READY
    implementation.save(update_fields=["status","updated_at"])
    completed=strategy.experiments.filter(role="EXECUTION",status="COMPLETED").count()
    features_ready=not strategy.feature_requirements.filter(required=True).exclude(feature__status="VALIDATED").exists()
    readiness,_=ResearchStrategyReadiness.objects.update_or_create(
        research_strategy=strategy,as_of_date=timezone.localdate(),
        defaults={"data_ready":completed>=1,
                  "features_ready":features_ready,"implementation_ready":implementation.exact_semantic_match,
                  "backtest_ready":completed>=1,"approved":has_eligible,
                  "builder_ready":implementation.status in {ImplementationStatus.BUILDER_READY,ImplementationStatus.APPROVED},
                  "blocking_reasons":[]},
    )
    blockers=[]
    if not readiness.data_ready:blockers.append("INSUFFICIENT_VALID_HISTORY")
    if not readiness.features_ready:blockers.append("FEATURES_NOT_VALIDATED")
    if not readiness.implementation_ready:blockers.append("NO_VALIDATED_IMPLEMENTATION")
    if not readiness.backtest_ready:blockers.append("NO_PASSING_BACKTEST")
    if not readiness.approved:blockers.append("NO_PASSING_SCORE")
    if not readiness.builder_ready:blockers.append("PAPER_VALIDATION_REQUIRED")
    readiness.blocking_reasons=blockers;readiness.save(update_fields=["blocking_reasons"])


def score_completed_trials():
    updated=0;experiments=ResearchTrial.objects.filter(
        status="COMPLETED",instrument__isnull=False,experiment__status="COMPLETED",experiment__role="EXECUTION"
    ).values_list("experiment_id",flat=True).distinct()
    strategies={}
    for experiment_id in experiments:
        trials=list(ResearchTrial.objects.filter(experiment_id=experiment_id,status="COMPLETED").select_related(
            "experiment__strategy","experiment__dataset_version","experiment__protocol","instrument"
        ))
        if not trials:continue
        ranked=sorted(trials,key=lambda trial:(
            _components(trial)["out_of_sample_sharpe"],trial.summary_metrics.get("cagr",-1),-trial.summary_metrics.get("max_drawdown",1)
        ),reverse=True)
        best=next((trial for trial in ranked if not candidate_score(_components(trial),_hard_metrics(trial))["hard_rejection_reasons"]),ranked[0])
        result=candidate_score(_components(best),_hard_metrics(best));strategy=best.experiment.strategy
        strategies[strategy.pk]=strategy
        validation=best.validation_metrics or {}
        for timeframe,risk_level in _compatible_pairs(strategy):
            ResearchCandidateScore.objects.update_or_create(
                strategy=strategy,instrument=best.instrument,goal_timeframe=timeframe,risk_level=risk_level,
                as_of_date=timezone.localdate(),
                defaults={"candidate_type":"EXECUTION","score":result["score"],"eligible":result["eligible"],
                          "hard_rejection_reasons":result["hard_rejection_reasons"],"best_parameters":best.parameters,
                          "metrics":best.summary_metrics,
                          "regime_metrics":{"consistency":validation.get("regime_consistency_score",0),
                                            "test_windows":validation.get("test_window_metrics",[]),
                                            "holdout":validation.get("holdout_result",{})},
                          "cost_metrics":{"expected_cost":best.summary_metrics.get("total_cost",0),
                                          "25bps_total_return":validation.get("cost_25bps_total_return"),
                                          "50bps_total_return":validation.get("cost_50bps_total_return"),
                                          "high_cost_net_return":validation.get("high_cost_net_return")},
                          "stability_metrics":{"stable":validation.get("parameter_neighborhood_stable",False),
                                               "score":validation.get("parameter_stability_score",0),
                                               "deflated_sharpe":validation.get("deflated_sharpe")},
                          "capacity_metrics":{"pass":validation.get("capacity_pass",False),
                                              "maximum_weight":validation.get("maximum_capacity_weight",0),
                                              "median_dollar_volume":validation.get("median_dollar_volume_252d",0)},
                          "protocol_version":best.experiment.protocol,"dataset_version":best.experiment.dataset_version,
                          "expires_at":timezone.now()+timedelta(days=settings.RESEARCH_SCORE_MAX_AGE_DAYS)},
            );updated+=1
    for strategy in strategies.values():
        current=ResearchCandidateScore.objects.filter(strategy=strategy,as_of_date=timezone.localdate())
        _update_lifecycle(strategy,current.exists(),current.filter(eligible=True).exists())
    return {"candidate_scores_updated":updated,"strategies_updated":len(strategies)}
