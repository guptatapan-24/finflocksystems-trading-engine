from django.apps import AppConfig
from django.core.checks import Error, Tags, register
from django.db import OperationalError, ProgrammingError


class ResearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.research"

    def ready(self):
        @register(Tags.models, deploy=True)
        def complete_strategy_registry(app_configs, **kwargs):
            from .models import ResearchDatasetVersion
            from .services.strategy_registry import validate_registry_for_dataset
            try:
                dataset = ResearchDatasetVersion.objects.filter(status="ACTIVE").order_by("-snapshot_date").first()
                if dataset:
                    validate_registry_for_dataset(dataset)
            except (OperationalError, ProgrammingError):
                return []
            except ValueError as exc:
                return [Error(str(exc), id="research.E001")]
            return []
