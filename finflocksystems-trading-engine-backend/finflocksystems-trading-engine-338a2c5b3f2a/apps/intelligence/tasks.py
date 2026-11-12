"""Celery tasks for the intelligence layer, all on queue `intelligence` (settings INTELLIGENCE_TASK_ROUTES).

Every task catches its own errors and returns quietly: intelligence must never affect trading or readiness (R2).
"""
import logging
from datetime import datetime, time, timezone as dt_timezone
from decimal import Decimal

from celery import shared_task
from django.utils import timezone

from . import conf

logger = logging.getLogger(__name__)
FILL_CURSOR = "fills"
SWEEP_BATCH = 200


def _rebuild(instance_ids):
    from .services.episodes import build_or_update_episodes

    done = 0
    for instance_id in sorted(set(instance_ids)):
        try:
            build_or_update_episodes(instance_id)
            done += 1
        except Exception:
            logger.exception("intelligence: episode build failed for instance %s", instance_id)
    return done


@shared_task(name="apps.intelligence.tasks.process_fill")
def process_fill(fill_id):
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .services.episodes import instances_for_fill

        return {"instances": _rebuild(instances_for_fill(fill_id))}
    except Exception:
        logger.exception("intelligence: process_fill failed for fill %s", fill_id)
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.sweep_fills")
def sweep_fills():
    """Recovery path: advances a cursor over new fills and also re-checks instances with OPEN episodes
    (which catches late commission corrections, since those do not create new fills)."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from apps.execution.models import Fill

        from .models import IntelCursor, TradeEpisode
        from .services.episodes import instances_for_fill

        cursor, _ = IntelCursor.objects.get_or_create(name=FILL_CURSOR)
        rows = list(Fill.objects.filter(pk__gt=cursor.last_id).order_by("pk").values_list("pk", flat=True)[:SWEEP_BATCH])
        instance_ids = set(TradeEpisode.objects.filter(status="OPEN").values_list("instance_id", flat=True))
        for fill_id in rows:
            instance_ids.update(instances_for_fill(fill_id))
        built = _rebuild(instance_ids)
        if rows:
            cursor.last_id = rows[-1]
            cursor.save(update_fields=["last_id", "updated_at"])
        return {"fills": len(rows), "instances": built}
    except Exception:
        logger.exception("intelligence: sweep_fills failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.sweep_operator_events")
def sweep_operator_events():
    """Journal what the engine recorded about the operator's choices: new versions, actions, manual orders (Phase 8)."""
    try:
        from .services.engine_events import sync

        return sync()
    except Exception:
        logger.exception("intelligence: sweep_operator_events failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.build_session_records")
def build_session_records():
    """Refresh today's and yesterday's session record per exchange; freeze them after the close (Phase 9)."""
    try:
        from .services import session, session_narrative

        return session.run_all(narrative_fn=session_narrative.for_record)
    except Exception:
        logger.exception("intelligence: build_session_records failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.poll_context")
def poll_context():
    """One tick of the outside-context poller (Phase 10): each source decides for itself whether it is due."""
    try:
        from .context import poller

        return poller.run()
    except Exception:
        logger.exception("intelligence: poll_context failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.build_daily_briefs")
def build_daily_briefs():
    """Pre-open brief per exchange, rebuilt until the exchange opens (Phase 10)."""
    try:
        from .services import daily_brief

        return daily_brief.run_all()
    except Exception:
        logger.exception("intelligence: build_daily_briefs failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.prune_context")
def prune_context():
    try:
        from django.conf import settings

        from .context import store

        if not conf.context_enabled():
            return {"skipped": "disabled"}
        return {"deleted": store.prune(days=int(getattr(settings, "INTELLIGENCE_CONTEXT_RETENTION_DAYS", 120)))}
    except Exception:
        logger.exception("intelligence: prune_context failed")
        return {"error": True}


# ------------------------------------------------------------------------------------------------
# expiry sweeper
# ------------------------------------------------------------------------------------------------
def _calendar_for(episode):
    return "XNSE" if episode.currency == "INR" else "XNYS"


def _market_today(calendar):
    from .services.metrics import _zone

    return timezone.now().astimezone(_zone(calendar)).date()


def _broker_holds(episode, open_legs):
    """True if the latest completed broker snapshot still shows any leg, False if flat or absent,
    None if there is no usable snapshot (then we wait and never guess)."""
    from apps.broker_gateway.models import BrokerPositionSnapshot
    from apps.instruments.models import BrokerContract
    from apps.portfolios.models import TradingPortfolio

    portfolio = TradingPortfolio.objects.filter(pk=episode.portfolio_id).first()
    if portfolio is None:
        return None
    snap = (BrokerPositionSnapshot.objects.filter(broker_account_id=portfolio.account_id, complete=True,
                                                  status="COMPLETED").order_by("-received_at").first())
    if snap is None or snap.received_at < episode.opened_at:
        return None
    conids = set(BrokerContract.objects.filter(instrument_id__in=[leg.instrument_id for leg in open_legs])
                 .values_list("conid", flat=True))
    for row in snap.positions or []:
        if not isinstance(row, dict):
            continue
        conid = row.get("conid") or row.get("con_id")
        qty = row.get("position", row.get("quantity", 0))
        try:
            if conid is not None and int(conid) in conids and Decimal(str(qty)) != 0:
                return True
        except Exception:
            continue
    return False


def _settlement_price(episode, expiry_end):
    from apps.market_streams.models import MarketBar

    bar = (MarketBar.objects.filter(instrument_id=episode.instrument_id, processing_mode__in=["LIVE", "WARMUP"],
                                    window_end__lte=expiry_end).order_by("-window_end", "-version").first())
    return bar.close if bar is not None else None


def _expire(episode):
    """Close one OPTION episode whose legs expired without a closing fill. Returns True if closed."""
    from .models import EpisodeEvidenceSnapshot, TradeEpisode
    from .services import evidence
    from .services.episodes import (
        EXPIRED_BROKER_STILL_HOLDS, EXPIRY_SETTLEMENT_UNKNOWN, PNL_ESTIMATED_AT_EXPIRY,
    )
    from .services.metrics import _zone

    legs = list(episode.legs.all())
    open_legs = [leg for leg in legs if leg.entry_quantity - leg.exit_quantity != 0]
    if not open_legs or any(leg.expiration is None for leg in open_legs):
        return False
    calendar = _calendar_for(episode)
    last_expiry = max(leg.expiration for leg in open_legs)
    if last_expiry >= _market_today(calendar):
        return False
    held = _broker_holds(episode, open_legs)
    if held is None:
        return False
    flags = set(episode.data_quality_flags or [])
    if held:
        if EXPIRED_BROKER_STILL_HOLDS not in flags:
            episode.data_quality_flags = sorted(flags | {EXPIRED_BROKER_STILL_HOLDS})
            episode.save(update_fields=["data_quality_flags", "updated_at"])
        return False
    expiry_end = datetime.combine(last_expiry, time(23, 59, 59), tzinfo=_zone(calendar)).astimezone(dt_timezone.utc)
    last_fill = max((leg.last_fill_at for leg in legs if leg.last_fill_at), default=episode.opened_at)
    settle = _settlement_price(episode, expiry_end)
    gross = episode.gross_pnl
    if settle is None:
        flags.add(EXPIRY_SETTLEMENT_UNKNOWN)
    else:
        flags.add(PNL_ESTIMATED_AT_EXPIRY)
        for leg in open_legs:
            remaining = leg.entry_quantity - leg.exit_quantity
            signed = remaining if leg.entry_side == "BUY" else -remaining
            intrinsic = max(Decimal(0), (settle - leg.strike) if leg.right == "C" else (leg.strike - settle))
            gross += (intrinsic - leg.entry_avg_price) * signed * leg.multiplier
    episode.status = TradeEpisode.Status.EXPIRED
    episode.exit_reason = "EXPIRED_NO_FILL"
    episode.closed_at = max(last_fill, expiry_end)
    episode.hold_seconds = int((episode.closed_at - episode.opened_at).total_seconds())
    episode.gross_pnl = gross
    episode.net_pnl = gross - episode.commissions
    episode.pnl_basis = "FILLS" if settle is None else "ESTIMATED_AT_EXPIRY"
    episode.data_quality_flags = sorted(flags)
    episode.save()
    metrics = getattr(episode, "metrics_row", None)
    if metrics is not None:
        data = dict(metrics.metrics)
        data.update({"exit.reason": "EXPIRED_NO_FILL", "outcome.is_closed": True,
                     "outcome.gross_pnl": float(episode.gross_pnl), "outcome.net_pnl": float(episode.net_pnl),
                     "outcome.hold_seconds": episode.hold_seconds, "flags": sorted(flags)})
        metrics.metrics = data
        metrics.save(update_fields=["metrics", "updated_at"])
    if not EpisodeEvidenceSnapshot.objects.filter(episode=episode).exists():
        pack, digest = evidence.build_expiry_pack(episode)
        EpisodeEvidenceSnapshot.objects.create(episode=episode, pack=pack, evidence_hash=digest,
                                               builder_version=evidence.PACK_VERSION)
        from .services.reviews import schedule_review

        schedule_review(episode.pk)
        from .services.learning import schedule_learning

        schedule_learning(episode.pk)
    return True


@shared_task(name="apps.intelligence.tasks.sweep_expired")
def sweep_expired():
    """Close OPTION episodes whose legs are past expiry, flat at the broker, and never closed by a fill
    (engine edge case: an ERROR-state instance never fires the expiry exit)."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    closed = 0
    try:
        from .models import TradeEpisode

        for episode in TradeEpisode.objects.filter(status="OPEN", asset_family="OPTION"):
            try:
                closed += 1 if _expire(episode) else 0
            except Exception:
                logger.exception("intelligence: expiry sweep failed for episode %s", episode.pk)
    except Exception:
        logger.exception("intelligence: sweep_expired failed")
    return {"closed": closed}


@shared_task(name="apps.intelligence.tasks.run_trade_review")
def run_trade_review(episode_id):
    """LLM post-trade review of one closed episode. Idempotent; never raises (fails open to a fallback review)."""
    if not conf.reviews_enabled():
        return {"skipped": "reviews disabled"}
    try:
        from .services.reviews import review_episode

        review = review_episode(episode_id)
        return {"review": getattr(review, "pk", None), "fallback": bool(getattr(review, "is_fallback", False))}
    except Exception:
        logger.exception("intelligence: review failed for episode %s", episode_id)
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.monitor_live")
def monitor_live():
    """Deterministic live monitor of OPEN episodes (no LLM, no gateway calls). Never raises."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .services.live import monitor_open_episodes

        return {"snapshots": monitor_open_episodes()}
    except Exception:
        logger.exception("intelligence: live monitor failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.prune_live_snapshots")
def prune_live_snapshots():
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .services.live import prune_snapshots

        return {"removed": prune_snapshots()}
    except Exception:
        logger.exception("intelligence: snapshot pruning failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.learn_from_episode")
def learn_from_episode(episode_id):
    """Update lessons, statistics and calibration after an episode closes. Idempotent; never raises."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .models import TradeEpisode
        from .services import analytics, learning

        episode = TradeEpisode.objects.filter(pk=episode_id).select_related("metrics_row").first()
        if episode is None or episode.status == TradeEpisode.Status.OPEN:
            return {"skipped": "not closed"}
        return {"lessons": learning.evaluate_episode(episode), "performance_rows": analytics.refresh_performance(),
                "calibration_rows": analytics.refresh_calibration()}
    except Exception:
        logger.exception("intelligence: learning failed for episode %s", episode_id)
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.daily_learning")
def daily_learning():
    """Retire stale lessons, resolve suggestion outcomes, write the digest to the log. Never raises."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .services import analytics, changepoint, digest, learning, playbook

        result = {"retired": learning.retire_stale_lessons(), "outcomes": analytics.resolve_suggestion_outcomes()}
        for name, fn in (("experiments", playbook.resolve_experiments), ("change_points", changepoint.refresh),
                         ("performance", analytics.refresh_performance), ("calibration", analytics.refresh_calibration)):
            try:  # one failing step never stops the others
                result[name] = fn()
            except Exception:
                logger.exception("intelligence: daily learning step %s failed", name)
                result[name] = "error"
        logger.info("intelligence daily digest: %s", digest.to_text(digest.build_digest(days=1)))
        return result
    except Exception:
        logger.exception("intelligence: daily learning failed")
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.run_weekly_review")
def run_weekly_review(strategy_key):
    """Aggregated review of one strategy. Fails open to a deterministic summary; never raises."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    try:
        from .services.weekly import weekly_review

        review = weekly_review(strategy_key)
        return {"review": getattr(review, "pk", None), "fallback": bool(getattr(review, "is_fallback", False))}
    except Exception:
        logger.exception("intelligence: weekly review failed for %s", strategy_key)
        return {"error": True}


@shared_task(name="apps.intelligence.tasks.weekly_reviews")
def weekly_reviews():
    """Daily check: review every strategy whose last weekly review is older than six days."""
    if not conf.reviews_enabled():
        return {"skipped": "reviews disabled"}
    try:
        from .services.weekly import due_strategies

        keys = due_strategies()
        for key in keys:
            run_weekly_review(key)
        return {"strategies": len(keys)}
    except Exception:
        logger.exception("intelligence: weekly review sweep failed")
        return {"error": True}
