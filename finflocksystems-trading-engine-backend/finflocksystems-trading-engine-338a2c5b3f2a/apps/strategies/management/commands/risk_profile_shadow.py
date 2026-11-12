import json

from django.core.management.base import BaseCommand

from apps.strategies import risk_profiles
from apps.strategies.models import StrategyDefinition, StrategyInstance
from apps.strategies.plugins import get_plugin


class Command(BaseCommand):
    help = (
        "Read-only shadow check. Prints how each risk profile differs from the legacy "
        "parameters (the definition defaults, and with --stored every existing strategy "
        "without a profile). Writes nothing and changes no behaviour."
    )

    def add_arguments(self, parser):
        parser.add_argument("--stored", action="store_true", help="also diff stored strategies")
        parser.add_argument("--definition", default="RPS_OPTIONS_AUTO_SELECTOR")

    def handle(self, *args, **options):
        definition = StrategyDefinition.objects.get(key=options["definition"].upper())
        legacy = get_plugin(definition).default_parameters
        for name in risk_profiles.PROFILE_NAMES:
            differences = risk_profiles.diff_parameters(legacy, risk_profiles.resolve_parameters(name))
            self.stdout.write(f"{name}: {json.dumps(differences, sort_keys=True)}")
        if options["stored"]:
            query = StrategyInstance.objects.filter(definition__key__in=risk_profiles.ELIGIBLE_DEFINITION_KEYS)
            for item in query.order_by("pk"):
                if (item.target_configuration or {}).get("risk_profile"):
                    continue
                differences = risk_profiles.diff_parameters(
                    item.parameters, risk_profiles.resolve_parameters(risk_profiles.DEFAULT_PROFILE)
                )
                self.stdout.write(f"#{item.pk} {item.name} (no profile): {json.dumps(differences, sort_keys=True, default=str)}")
