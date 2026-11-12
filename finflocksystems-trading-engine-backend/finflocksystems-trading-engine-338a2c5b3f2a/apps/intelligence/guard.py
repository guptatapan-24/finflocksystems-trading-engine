"""R6: refuse the startup flush when intelligence data exists.

`manage.py flush` truncates every table, resets sequences, and engine ids get reused, so intel_ rows would
silently point at different strategies afterwards. We never exclude intel_ tables from the flush (order-sensitive
TRUNCATE, id reuse); we refuse instead, and `export_intelligence_dataset` is the backup path.
"""
from django.apps import apps as django_apps

OVERRIDE_FLAG = "--i-understand-intelligence-data-loss"


def intel_models():
    return [m for m in django_apps.get_app_config("intelligence").get_models()]


def intel_row_counts():
    """{table: count} for every intel_ table that has at least one row."""
    counts = {}
    for model in intel_models():
        try:
            n = model.objects.count()
        except Exception:  # table missing (not migrated yet): nothing to protect
            continue
        if n:
            counts[model._meta.db_table] = n
    return counts


def flush_refusal_message():
    """Return None when a flush is safe, else a human-readable refusal.

    Stricter than 'flag on AND rows': any intel_ row blocks the flush, even with INTELLIGENCE_ENABLED=false,
    because rows written earlier would be destroyed just the same.
    """
    counts = intel_row_counts()
    if not counts:
        return None
    detail = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    return (
        "Refusing to flush the database: intelligence tables contain data "
        f"({detail}). A flush would destroy lessons, reviews and statistics. "
        "Back up the data first (the export command arrives in Phase 5), then either set "
        "CLEANUP_DATABASE_ON_START=false or pass "
        f"{OVERRIDE_FLAG} to flush on purpose."
    )
