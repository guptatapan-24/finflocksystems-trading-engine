import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def _enqueue_fill(fill_id):
    """Runs after the engine's transaction commits. Never raises, never loops on retries (R2, R13)."""
    try:
        from .tasks import process_fill

        process_fill.apply_async((fill_id,), retry=False, ignore_result=True)
    except Exception:
        # Broker down or misconfigured: the 30 s sweep is the recovery path.
        logger.warning("intelligence: could not enqueue fill %s; sweep will pick it up", fill_id, exc_info=True)


def _on_fill_saved(sender, instance, **kwargs):
    """post_save(Fill): only registers an after-commit enqueue. No queries, no engine writes."""
    try:
        from django.conf import settings
        from django.db import transaction

        if not getattr(settings, "INTELLIGENCE_ENABLED", False):
            return
        fill_id = instance.pk
        transaction.on_commit(lambda: _enqueue_fill(fill_id))
    except Exception:
        logger.exception("intelligence: fill hook failed")


class IntelligenceConfig(AppConfig):
    name = "apps.intelligence"
    label = "intelligence"
    verbose_name = "Trading intelligence (advisory only)"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.execution.models import Fill

        post_save.connect(_on_fill_saved, sender=Fill, dispatch_uid="intelligence.fill_saved")
