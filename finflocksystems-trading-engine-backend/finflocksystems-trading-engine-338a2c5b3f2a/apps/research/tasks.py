from __future__ import annotations

from contextlib import contextmanager

from celery import shared_task
from django.conf import settings
from django.core.cache import cache


@contextmanager
def _bounded_lock(name, timeout=3600):
    key = f"research:lock:{name}"
    acquired = cache.add(key, "1", timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


@shared_task
def refresh_universe_mapping(offset=0, batch_size=50):
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.universe_pipeline import map_universe_batch
    with _bounded_lock(f"mapping:{offset}") as acquired:
        if not acquired:return {"status":"LOCKED"}
        size=min(batch_size,settings.RESEARCH_MAX_PARALLEL_DATA_TASKS*10)
        result=map_universe_batch(offset=offset,batch_size=size)
        from .services.universe_pipeline import active_recommendation_universe
        total=active_recommendation_universe().members.filter(active=True).count()
        if result["next_offset"]<total:refresh_universe_mapping.delay(result["next_offset"],size)
        return {**result,"total":total}


@shared_task
def refresh_research_pipeline(offset=0, batch_size=25):
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.universe_pipeline import refresh_data_batch
    with _bounded_lock(f"daily:{offset}", timeout=3 * 3600) as acquired:
        if not acquired:return {"status":"LOCKED"}
        result=refresh_data_batch(offset=offset,batch_size=min(batch_size,50))
        from .services.universe_pipeline import active_recommendation_universe
        total=active_recommendation_universe().members.filter(active=True,instrument__isnull=False).count()
        if result["next_offset"]<total:refresh_research_pipeline.delay(result["next_offset"],batch_size)
        else:calculate_features.delay()
        return {**result,"total":total}


@shared_task
def refresh_intraday_data(offset=0, batch_size=10):
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.research_data import refresh_intraday_history
    from .services.observability import record_pipeline_failure
    from .services.universe_pipeline import active_recommendation_universe, update_coverage
    members = list(active_recommendation_universe().members.filter(
        active=True, instrument__isnull=False,
    ).select_related("instrument", "issuer").order_by("pk")[offset:offset + min(batch_size, 25)])
    rows = []
    with _bounded_lock(f"intraday:{offset}", timeout=3 * 3600) as acquired:
        if not acquired:
            return {"status": "LOCKED"}
        for member in members:
            try:
                rows.append(refresh_intraday_history(member.instrument, frequency="1h", days=settings.RESEARCH_INTRADAY_LOOKBACK_DAYS))
                update_coverage(member)
            except Exception as exc:
                rows.append({"symbol": member.source_symbol, "error": str(exc)[:500]})
                record_pipeline_failure("intraday_data", member.pk, exc, symbol=member.source_symbol)
    next_offset=offset+len(rows);total=active_recommendation_universe().members.filter(
        active=True,instrument__isnull=False).count()
    if rows and next_offset<total:refresh_intraday_data.delay(next_offset,batch_size)
    return {"processed":len(rows),"next_offset":next_offset,"total":total,"results":rows}


@shared_task
def refresh_fundamentals(offset=0, batch_size=10):
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.point_in_time_data import refresh_fundamentals as refresh_member
    from .services.observability import record_pipeline_failure
    from .services.universe_pipeline import active_recommendation_universe, update_coverage
    members = list(active_recommendation_universe().members.filter(active=True, instrument__isnull=False).select_related("instrument", "issuer").order_by("pk")[offset:offset + batch_size])
    rows=[]
    for member in members:
        try:
            rows.append({"symbol":member.source_symbol,**refresh_member(member)});update_coverage(member)
        except Exception as exc:
            rows.append({"symbol":member.source_symbol,"error":str(exc)[:500]})
            record_pipeline_failure("fundamentals",member.pk,exc,symbol=member.source_symbol)
    next_offset=offset+len(rows);total=active_recommendation_universe().members.filter(active=True,instrument__isnull=False).count()
    if rows and next_offset<total:refresh_fundamentals.delay(next_offset,batch_size)
    return {"processed":len(rows),"next_offset":next_offset,"total":total,"results":rows}


@shared_task
def refresh_events(offset=0, batch_size=10):
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.point_in_time_data import refresh_analyst_and_events
    from .services.observability import record_pipeline_failure
    from .services.universe_pipeline import active_recommendation_universe, update_coverage
    members=list(active_recommendation_universe().members.filter(active=True,instrument__isnull=False).select_related("instrument","issuer").order_by("pk")[offset:offset+batch_size])
    rows=[]
    for member in members:
        try:
            rows.append({"symbol":member.source_symbol,**refresh_analyst_and_events(member)});update_coverage(member)
        except Exception as exc:
            rows.append({"symbol":member.source_symbol,"error":str(exc)[:500]})
            record_pipeline_failure("events",member.pk,exc,symbol=member.source_symbol)
    next_offset=offset+len(rows);total=active_recommendation_universe().members.filter(active=True,instrument__isnull=False).count()
    if rows and next_offset<total:refresh_events.delay(next_offset,batch_size)
    return {"processed":len(rows),"next_offset":next_offset,"total":total,"results":rows}


@shared_task
def calculate_features():
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.feature_pipeline import precompute_common_features
    from .services.universe_pipeline import active_recommendation_universe
    with _bounded_lock("features", timeout=3 * 3600) as acquired:
        if not acquired:return {"status":"LOCKED"}
        result=precompute_common_features(active_recommendation_universe())
        schedule_research_experiments.delay()
        return result


@shared_task
def schedule_research_experiments():
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .services.experiment_factory import build_role_aware_experiments
    from .services.universe_pipeline import active_recommendation_universe
    with _bounded_lock("experiment-factory", timeout=3600) as acquired:
        if not acquired:return {"status":"LOCKED"}
        result=build_role_aware_experiments(active_recommendation_universe())
        dispatch_research_experiments.delay()
        return result


@shared_task
def dispatch_research_experiments(limit=None):
    if not settings.RESEARCH_ENABLED:
        return {"status":"DISABLED"}
    from django.db import transaction
    from django.utils import timezone
    from datetime import timedelta
    from .models import ResearchExperiment
    maximum=min(int(limit or settings.RESEARCH_MAX_PARALLEL_BACKTEST_TASKS*4),100)
    ResearchExperiment.objects.filter(
        status="RUNNING",started_at__lt=timezone.now()-timedelta(hours=6)
    ).update(status="QUEUED",started_at=None,error="Recovered stale experiment dispatch")
    with transaction.atomic():
        experiments=list(ResearchExperiment.objects.select_for_update(skip_locked=True).filter(
            status="QUEUED").order_by("pk")[:maximum])
        ResearchExperiment.objects.filter(pk__in=[item.pk for item in experiments]).update(status="RUNNING",started_at=timezone.now())
    task_by_engine={
        "SINGLE_ASSET":run_single_asset_experiment,"CROSS_SECTIONAL":run_cross_sectional_experiment,
        "ALLOCATOR":run_allocator_experiment,"OVERLAY":run_overlay_experiment,
        "EVENT":run_event_experiment,"PAIR_BASKET":run_pair_experiment,
    }
    dispatched=[]
    for experiment in experiments:
        try:
            task_by_engine[experiment.experiment_type].delay(experiment.pk);dispatched.append(experiment.pk)
        except Exception:
            ResearchExperiment.objects.filter(pk=experiment.pk,status="RUNNING").update(status="QUEUED",started_at=None)
            raise
    if not experiments and not ResearchExperiment.objects.filter(status="RUNNING").exists():
        score_current_candidates.delay()
    return {"dispatched":len(dispatched),"experiment_ids":dispatched}


def _execute_role(experiment_id, expected_engine):
    if not settings.RESEARCH_ENABLED:
        return {"status":"DISABLED","experiment_id":experiment_id}
    from .models import ResearchExperiment
    from .services.experiment_runner import run_experiment
    experiment=ResearchExperiment.objects.get(pk=experiment_id)
    if experiment.experiment_type!=expected_engine:
        raise ValueError(f"Experiment {experiment_id} belongs to {experiment.experiment_type}, not {expected_engine}")
    result=run_experiment(experiment)
    dispatch_research_experiments.delay()
    return result


@shared_task
def run_single_asset_experiment(experiment_id):
    return _execute_role(experiment_id,"SINGLE_ASSET")


@shared_task
def run_cross_sectional_experiment(experiment_id):
    return _execute_role(experiment_id,"CROSS_SECTIONAL")


@shared_task
def run_allocator_experiment(experiment_id):
    return _execute_role(experiment_id,"ALLOCATOR")


@shared_task
def run_overlay_experiment(experiment_id):
    return _execute_role(experiment_id,"OVERLAY")


@shared_task
def run_event_experiment(experiment_id):
    return _execute_role(experiment_id,"EVENT")


@shared_task
def run_pair_experiment(experiment_id):
    return _execute_role(experiment_id,"PAIR_BASKET")


@shared_task
def score_current_candidates():
    if not settings.RESEARCH_ENABLED:
        return {"status": "DISABLED"}
    from .models import ResearchDatasetVersion
    from .services.candidate_service import score_completed_trials
    from .services.recommendation_cache import calculate_role_scores
    result=score_completed_trials();dataset=ResearchDatasetVersion.objects.filter(status="ACTIVE").first()
    output={**result,"roles":calculate_role_scores(dataset) if dataset else {}}
    warm_recommendation_cache.delay()
    return output


@shared_task
def warm_recommendation_cache():
    if not settings.RECOMMENDATION_SYSTEM_ENABLED:
        return {"status": "DISABLED"}
    from .models import ResearchDatasetVersion
    if not ResearchDatasetVersion.objects.filter(status="ACTIVE").exists():
        # A fresh deployment can legitimately run beat before research bootstrap.
        # Treat that state as not ready instead of failing a scheduled Celery task.
        return {"status": "WAITING_FOR_ACTIVE_DATASET"}
    from .services.recommendation_cache import warm_all_recommendation_caches
    with _bounded_lock("recommendation-cache", timeout=3 * 3600) as acquired:
        return warm_all_recommendation_caches() if acquired else {"status": "LOCKED"}


@shared_task
def generate_recommendation_batch(batch_id):
    from .services.recommendation_batch import run_recommendation_batch
    batch=run_recommendation_batch(batch_id)
    return {"batch_id":batch.pk,"status":batch.status}
