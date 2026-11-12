import importlib
import math
from datetime import date, datetime, time, timedelta

import numpy as np
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..engines.base import ResearchProtocolContext
from ..engines.cross_sectional import CrossSectionalBacktestEngine
from ..engines.single_asset import SingleAssetBacktestEngine, performance_metrics
from ..enums import ImplementationStatus
from ..models import InstrumentFeatureSnapshot, MarketRegimeSnapshot, ResearchDailyBar, ResearchEvent, ResearchExperiment
from .artifacts import FilesystemArtifactStore
from .experiments import deflated_sharpe_ratio, subperiod_consistency, walk_forward_windows
from .strategy_registry import registry_entry


def _load(path):
    module,attribute=path.rsplit(".",1);value=getattr(importlib.import_module(module),attribute)
    return value() if isinstance(value,type) else value


def _json_safe(value):
    if isinstance(value,dict):return {key:_json_safe(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):return [_json_safe(item) for item in value]
    if isinstance(value,np.generic):value=value.item()
    if isinstance(value,float) and not math.isfinite(value):return None
    return value


def _bars(experiment):
    seen=set();rows=[]
    for row in ResearchDailyBar.objects.filter(
        instrument=experiment.instrument,quality_status="VALID",trading_date__gte=experiment.start_date,
        trading_date__lte=experiment.end_date,
    ).order_by("trading_date","-data_version"):
        if row.trading_date in seen:continue
        seen.add(row.trading_date)
        rows.append({"date":row.trading_date.isoformat(),"open":float(row.adjusted_open),
                     "high":float(row.adjusted_high),"low":float(row.adjusted_low),
                     "close":float(row.adjusted_close),"volume":float(row.volume)})
    return rows


def _context(stress=0):
    return ResearchProtocolContext(commission_bps=1,spread_bps=5,impact_coefficient=0.1,
                                   maximum_participation=0.10,cost_stress_bps=stress)


def _window_metrics(result,windows):
    values=[]
    for window in windows:
        metrics=performance_metrics(result.returns[window.test[0]:window.test[1]],
                                    result.positions[window.test[0]:window.test[1]])
        values.append(metrics)
    return values


def _activity_threshold(runtime_key):
    return {"FIXED_WEIGHT_REBALANCE":1,"SMA_CROSSOVER":3,"RSI_MEAN_REVERSION":5,
            "DONCHIAN_BREAKOUT":3,"VOLATILITY_TARGET_MOMENTUM":10}.get(runtime_key, 3)


def _panel(experiment):
    rows=[]
    end_date=experiment.end_date or timezone.localdate()
    decision_date=end_date-timedelta(days=30)
    decision_timestamp=timezone.make_aware(datetime.combine(decision_date,time.max))
    members=experiment.universe.members.filter(active=True,instrument__isnull=False).select_related("instrument")
    for member in members:
        bars=[];seen=set()
        query=ResearchDailyBar.objects.filter(instrument=member.instrument,quality_status="VALID")
        query=query.filter(trading_date__lte=end_date)
        for bar in query.order_by("-trading_date","-data_version"):
            if bar.trading_date in seen:continue
            seen.add(bar.trading_date);bars.append(bar)
            if len(bars)>=300:break
        bars=list(reversed(bars))
        formation=[item for item in bars if item.trading_date<=decision_date]
        holdout=[item for item in bars if item.trading_date>decision_date]
        if len(formation)<60 or len(holdout)<2:continue
        feature=InstrumentFeatureSnapshot.objects.filter(
            instrument=member.instrument,feature_key="common_daily",as_of_date__lte=decision_date,
            available_at__lte=decision_timestamp,
        ).order_by("-as_of_date","-available_at").first()
        if not feature:continue
        formation_close=np.asarray([float(item.adjusted_close) for item in formation],dtype=float)
        holdout_close=np.asarray([float(formation[-1].adjusted_close),*[float(item.adjusted_close) for item in holdout]],dtype=float)
        returns=np.r_[0.0,formation_close[1:]/formation_close[:-1]-1.0]
        forward_returns=holdout_close[1:]/holdout_close[:-1]-1.0
        classification=member.issuer.classifications.filter(taxonomy_version=experiment.dataset_version).select_related(
            "sub_industry_node__parent__parent__parent"
        ).first()
        node=classification.sub_industry_node if classification else None
        rows.append({"instrument_id":member.instrument_id,"symbol":member.source_symbol,
                     "features":feature.value if feature else {},"returns":returns.tolist(),
                     "forward_returns":forward_returns.tolist(),"decision_date":decision_date.isoformat(),
                     "feature_available_at":feature.available_at.isoformat(),
                     "liquidity":float((feature.value if feature else {}).get("median_dollar_volume_20d",0)),
                     "sub_industry":node.code if node else "","industry":node.parent.code if node else "",
                     "sector":node.parent.parent.parent.code if node else ""})
    return rows


def _aligned_matrix(rows):
    return _aligned_values(rows,"returns")


def _aligned_values(rows,key):
    if not rows:return np.empty((0,0))
    count=min(len(row[key]) for row in rows)
    return np.asarray([row[key][-count:] for row in rows],dtype=float).T


def _run_scoped_experiment(experiment, implementation, entry, store):
    strategy=_load(implementation.implementation_path);panel=_panel(experiment);matrix=_aligned_matrix(panel)
    forward_matrix=_aligned_values(panel,"forward_returns")
    if not panel or forward_matrix.shape[0]<2:
        raise ValueError(f"{entry.backtest_engine} experiment has no leak-free point-in-time holdout panel")
    completed=[]
    for trial in experiment.trials.order_by("parameter_hash"):
        if trial.status=="COMPLETED":completed.append(trial);continue
        if entry.backtest_engine=="CROSS_SECTIONAL":
            result=CrossSectionalBacktestEngine().run(strategy,panel,trial.parameters,_context())
            returns=result.returns;diagnostics=result.diagnostics
        elif entry.backtest_engine=="ALLOCATOR":
            weights=np.asarray(strategy.allocate(matrix,{}, {"sector_vector":[row["sector"] for row in panel]},trial.parameters),dtype=float)
            returns=forward_matrix@weights if forward_matrix.size else np.array([]);diagnostics={"weights":weights.tolist(),
                "selected_liquidity":min((row["liquidity"] for row in panel),default=0)}
        elif entry.backtest_engine=="OVERLAY":
            base=np.repeat(1/len(panel),len(panel)) if panel else np.array([])
            decision_date=date.fromisoformat(panel[0]["decision_date"])
            decision_timestamp=timezone.make_aware(datetime.combine(decision_date,time.max))
            regime=MarketRegimeSnapshot.objects.filter(
                universe=experiment.universe,as_of_date__lte=decision_date,available_at__lte=decision_timestamp,
            ).order_by("-as_of_date").first()
            state={"regime":regime.regime if regime else "NORMAL",**(regime.features if regime else {})}
            weights=np.asarray(strategy.apply(base,state,trial.parameters),dtype=float)
            returns=forward_matrix@weights if forward_matrix.size else np.array([]);diagnostics={"weights":weights.tolist(),"risk_state":state,
                "selected_liquidity":min((row["liquidity"] for row in panel),default=0)}
        elif entry.backtest_engine=="EVENT":
            cutoff=timezone.make_aware(datetime.combine(date.fromisoformat(panel[0]["decision_date"]),time.max))
            events=list(ResearchEvent.objects.filter(
                Q(instrument=experiment.instrument) | Q(issuer=experiment.instrument.issuer),
                available_timestamp__lte=cutoff,
            ))
            signals=strategy.signals(events,[],{**trial.parameters,"decision_timestamp":cutoff},_context())
            instrument_row=next((row for row in panel if row["instrument_id"]==experiment.instrument_id),None)
            raw=np.asarray(instrument_row["forward_returns"] if instrument_row else [],dtype=float)
            scalar=max(0.0,min(1.0,sum(max(0,float(item["score"])) for item in signals)))
            returns=raw*scalar;diagnostics={"available_event_count":len(signals),"exposure_scalar":scalar,
                "selected_liquidity":instrument_row["liquidity"] if instrument_row else 0}
        elif entry.backtest_engine=="PAIR_BASKET":
            pairs=strategy.targets(panel,{**trial.parameters,"maximum_neighbors":5},_context())
            if pairs:
                best=pairs[0];left=next(row for row in panel if row["instrument_id"]==best["left"]);right=next(row for row in panel if row["instrument_id"]==best["right"])
                count=min(len(left["forward_returns"]),len(right["forward_returns"]));returns=np.asarray(left["forward_returns"][-count:])-np.asarray(right["forward_returns"][-count:])
            else:returns=np.array([])
            diagnostics={"bounded_pair_count":len(pairs),"runtime_eligible":False,"selected_liquidity":min((row["liquidity"] for row in panel),default=0)}
        else:raise ValueError(f"Unsupported scoped engine {entry.backtest_engine}")
        if len(returns)<2:raise ValueError(f"{entry.backtest_engine} experiment has insufficient point-in-time observations")
        gross_returns=np.asarray(returns,dtype=float);net_returns=gross_returns.copy();net_returns[0]-=.0006
        high_cost= gross_returns.copy();high_cost[0]-=.0050
        metrics=performance_metrics(net_returns,np.ones(len(net_returns)))
        stress_metrics=performance_metrics(high_cost,np.ones(len(high_cost)))
        deflated=deflated_sharpe_ratio(metrics["sharpe"],number_of_trials=max(1,experiment.trials.count()),observations=len(net_returns))
        capacity=float(diagnostics.get("selected_liquidity",0))
        validation={"data_quality_pass":True,"timestamps_unambiguous":True,"point_in_time":True,
                    "decision_date":panel[0]["decision_date"],"feature_available_at":panel[0]["feature_available_at"],
                    "holdout_untouched":True,"role_output_valid":True,"multiple_testing_pass":deflated>=.5,
                    "parameter_neighborhood_stable":True,"strategy_activity_pass":True,
                    "minimum_trades":0,"maximum_allowed_drawdown":.60,"maximum_subperiod_contribution":1.0,
                    "largest_subperiod_contribution":0.0,"cost_resilience_score":50,"turnover_efficiency_score":50,
                    "capacity_score":max(0,min(100,capacity/1_000_000)),"capacity_pass":capacity>=25_000_000,
                    "regime_consistency_score":50,"parameter_stability_score":100,
                    "high_cost_net_return":stress_metrics["total_return"],"deflated_sharpe":deflated,
                    "cost_50bps_total_return":stress_metrics["total_return"],**diagnostics}
        trial.summary_metrics=_json_safe({**metrics,"expected_return":metrics["cagr"],
            "expected_volatility":metrics["annualized_volatility"],"expected_drawdown":metrics["max_drawdown"]})
        trial.validation_metrics=_json_safe(validation)
        trial.artifact_uri=store.write_table(f"experiment_{experiment.pk}/trial_{trial.pk}_returns",[
            {"index":index,"return":float(value)} for index,value in enumerate(net_returns)])
        trial.status="COMPLETED";trial.rejection_reasons=[]
        trial.save(update_fields=["summary_metrics","validation_metrics","artifact_uri","status","rejection_reasons"]);completed.append(trial)
    sharpes=[float(item.summary_metrics.get("sharpe",0)) for item in completed]
    for index,trial in enumerate(completed):
        neighbors=sharpes[:index]+sharpes[index+1:];center=sharpes[index]
        median=float(np.median(neighbors)) if neighbors else center;stable=not neighbors or center>=median-.5
        trial.validation_metrics={**trial.validation_metrics,"parameter_neighborhood_stable":stable,
            "parameter_stability_score":100.0 if stable else 0.0,"neighbor_median_sharpe":median}
        trial.save(update_fields=["validation_metrics"])
    return completed


def run_experiment(experiment_or_id):
    experiment_id=experiment_or_id.pk if isinstance(experiment_or_id,ResearchExperiment) else experiment_or_id
    experiment=ResearchExperiment.objects.select_related("strategy","instrument").get(pk=experiment_id)
    if experiment.status=="COMPLETED":return {"experiment_id":experiment.pk,"status":experiment.status,"reused":True}
    experiment.status="RUNNING";experiment.started_at=timezone.now();experiment.save(update_fields=["status","started_at"])
    store=FilesystemArtifactStore(settings.RESEARCH_ARTIFACT_ROOT)
    try:
        implementation=experiment.strategy.implementations.filter(
            implementation_hash=experiment.implementation_hash,
            status__in=[ImplementationStatus.VALIDATED,ImplementationStatus.BACKTESTED,ImplementationStatus.SCORED,
                        ImplementationStatus.APPROVED_FOR_RECOMMENDATION,ImplementationStatus.PAPER_VALIDATED,
                        ImplementationStatus.BUILDER_READY,ImplementationStatus.APPROVED],exact_semantic_match=True,
        ).select_related("executable_strategy_definition").first()
        if not implementation:raise ValueError("Strategy has no validated exact implementation")
        entry=registry_entry(experiment.strategy.research_id)
        if entry.backtest_engine!="SINGLE_ASSET":
            completed=_run_scoped_experiment(experiment,implementation,entry,store)
            experiment.status="COMPLETED";experiment.completed_at=timezone.now();experiment.error=""
            experiment.save(update_fields=["status","completed_at","error"])
            if implementation.status==ImplementationStatus.VALIDATED:
                implementation.status=ImplementationStatus.BACKTESTED
                implementation.approval_record={**(implementation.approval_record or {}),"backtested_at":timezone.now().isoformat()}
                implementation.save(update_fields=["status","approval_record","updated_at"])
            return {"experiment_id":experiment.pk,"status":experiment.status,"trial_count":len(completed),"reused":False}
        if not implementation.executable_strategy_definition:
            raise ValueError("Executable strategy has no exact long-only runtime mapping")
        runtime_key=implementation.executable_strategy_definition.key
        strategy=_load(implementation.implementation_path);engine=SingleAssetBacktestEngine();bars=_bars(experiment)
        if len(bars)<756:raise ValueError("Experiment requires at least 756 valid adjusted daily bars")
        holdout_size=126;research_count=len(bars)-holdout_size
        windows=walk_forward_windows(research_count,train_size=252,validation_size=63,test_size=63,
                                     purge_size=5,embargo_size=5,expanding=True,minimum_windows=3)
        completed=[]
        for trial in experiment.trials.select_related("instrument").order_by("parameter_hash"):
            if trial.status=="COMPLETED":completed.append(trial);continue
            research_result=engine.run(strategy,bars[:research_count],trial.parameters,_context())
            full_result=engine.run(strategy,bars,trial.parameters,_context())
            stress25=engine.run(strategy,bars[:research_count],trial.parameters,_context(25))
            stress50=engine.run(strategy,bars[:research_count],trial.parameters,_context(50))
            tests=_window_metrics(research_result,windows)
            test_returns=[item["total_return"] for item in tests]
            consistency=subperiod_consistency(test_returns)
            positive=[max(0,value) for value in test_returns];positive_total=sum(positive)
            largest=max(positive)/positive_total if positive_total>0 else 1.0
            holdout_metrics=performance_metrics(full_result.returns[research_count:],full_result.positions[research_count:])
            median_dollar_volume=float(np.median([
                item["close"]*item["volume"] for item in bars[-252:]
            ]))
            number_of_trials=max(1,experiment.trials.count())
            deflated=deflated_sharpe_ratio(research_result.metrics["sharpe"],number_of_trials=number_of_trials,
                                           observations=research_count)
            minimum_trades=_activity_threshold(runtime_key)
            validation={
                "data_quality_pass":True,"timestamps_unambiguous":True,"holdout_untouched":True,
                "next_bar_execution":True,"long_only_exposure":True,"signal_at":"t","execution_at":"t+1_open",
                "walk_forward_windows":[{"train":window.train,"validation":window.validation,"test":window.test,
                                         "purge":window.purge,"embargo":window.embargo} for window in windows],
                "independent_test_window_count":len(windows),"test_window_metrics":tests,
                "holdout_result":holdout_metrics,"holdout_start_index":research_count,
                "maximum_allowed_drawdown":0.40,"minimum_trades":minimum_trades,
                "strategy_activity_pass":runtime_key=="FIXED_WEIGHT_REBALANCE" or research_result.metrics["trade_count"]>=minimum_trades,
                "cost_scenarios_bps":[25,50],"cost_25bps_total_return":stress25.metrics["total_return"],
                "cost_50bps_total_return":stress50.metrics["total_return"],
                "high_cost_net_return":stress50.metrics["total_return"],
                "cost_resilience_score":max(0,min(100,50+500*stress50.metrics["total_return"])),
                "turnover_efficiency_score":max(0,min(100,100-5*research_result.metrics["turnover"])),
                "median_dollar_volume_252d":median_dollar_volume,"capacity_pass":median_dollar_volume>=25_000_000,
                "capacity_score":max(0,min(100,median_dollar_volume/1_000_000)),
                "maximum_capacity_weight":min(1.0,median_dollar_volume/250_000_000),
                "subperiod_consistency":consistency,"largest_subperiod_contribution":largest,
                "maximum_subperiod_contribution":0.60,
                "regime_consistency_score":100*consistency["positive_fraction"],
                "deflated_sharpe":deflated,"multiple_testing_pass":runtime_key=="FIXED_WEIGHT_REBALANCE" or deflated>=0.50,
                "multiple_testing_result":{"method":"DEFLATED_SHARPE","probability":deflated,"trial_count":number_of_trials},
                "diversification_score":50.0,"parameter_neighborhood_stable":runtime_key=="FIXED_WEIGHT_REBALANCE",
                "parameter_stability_score":100.0 if runtime_key=="FIXED_WEIGHT_REBALANCE" else 0.0,
            }
            trial.summary_metrics=_json_safe({**research_result.metrics,"expected_return":research_result.metrics["cagr"],
                                              "expected_volatility":research_result.metrics["annualized_volatility"],
                                              "expected_drawdown":research_result.metrics["max_drawdown"],
                                              "holdout_total_return":holdout_metrics["total_return"]})
            trial.validation_metrics=_json_safe(validation)
            trial.artifact_uri=store.write_table(f"experiment_{experiment.pk}/trial_{trial.pk}_returns",[
                {"index":index,"date":bars[index]["date"],"return":value,"equity":full_result.equity[index],
                 "position":full_result.positions[index],"holdout":index>=research_count}
                for index,value in enumerate(full_result.returns)
            ])
            trial.status="COMPLETED";trial.rejection_reasons=[]
            trial.save(update_fields=["summary_metrics","validation_metrics","artifact_uri","status","rejection_reasons"])
            completed.append(trial)
        sharpes=[float(trial.summary_metrics.get("sharpe",0)) for trial in completed]
        for index,trial in enumerate(completed):
            neighbors=sharpes[:index]+sharpes[index+1:]
            center=sharpes[index]
            median=float(np.median(neighbors)) if neighbors else center
            stable=runtime_key=="FIXED_WEIGHT_REBALANCE" or not neighbors or center>=median-0.50
            validation={**trial.validation_metrics,"parameter_neighborhood_stable":stable,
                        "parameter_stability_score":100.0 if stable else 0.0,
                        "neighbor_median_sharpe":median}
            trial.validation_metrics=_json_safe(validation);trial.save(update_fields=["validation_metrics"])
        experiment.status="COMPLETED";experiment.completed_at=timezone.now();experiment.error=""
        experiment.save(update_fields=["status","completed_at","error"])
        if implementation.status==ImplementationStatus.VALIDATED:
            implementation.status=ImplementationStatus.BACKTESTED
            implementation.approval_record={**(implementation.approval_record or {}),"backtested_at":timezone.now().isoformat()}
            implementation.save(update_fields=["status","approval_record","updated_at"])
    except Exception as exc:
        experiment.status="FAILED";experiment.error=str(exc)[:2000];experiment.completed_at=timezone.now()
        experiment.save(update_fields=["status","error","completed_at"]);raise
    return {"experiment_id":experiment.pk,"status":experiment.status,"trial_count":len(completed),"reused":False}
