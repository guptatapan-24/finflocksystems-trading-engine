from __future__ import annotations

import logging

from django.utils import timezone

from apps.audit.models import AuditEvent


logger = logging.getLogger("finflock.research")


def record_pipeline_failure(stage, aggregate_id, error, *, symbol=""):
    message = str(error)[:1000]
    logger.error("research pipeline failure", extra={
        "research_stage": stage, "aggregate_id": str(aggregate_id), "symbol": symbol,
        "error": message,
    })
    bucket = timezone.now().strftime("%Y%m%d%H")
    AuditEvent.objects.get_or_create(
        idempotency_key=f"research-failure:{stage}:{aggregate_id}:{bucket}"[:128],
        defaults={
            "event_type": "research.provider.failure", "actor": "recommendation-system",
            "aggregate_type": "research_universe_member", "aggregate_id": str(aggregate_id),
            "data": {"stage": stage, "symbol": symbol, "error": message},
        },
    )
