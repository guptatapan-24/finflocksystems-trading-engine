import os
from celery import Celery
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("trading_engine")
app.config_from_object("django.conf:settings", namespace="CELERY")
# Celery gives a raw CELERY_BROKER_URL environment variable priority over app configuration, which would silently drop
# the REDIS_PASSWORD that config/settings.py injects into the URL. Publish the resolved URL so workers and beat use it.
from django.conf import settings  # noqa: E402

os.environ["CELERY_BROKER_URL"] = settings.CELERY_BROKER_URL
app.autodiscover_tasks()

