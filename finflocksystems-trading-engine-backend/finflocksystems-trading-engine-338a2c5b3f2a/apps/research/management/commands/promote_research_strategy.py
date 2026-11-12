import json

from django.core.management.base import BaseCommand, CommandError

from ...models import ResearchStrategyDefinition
from ...services.promotion import promote_strategy


class Command(BaseCommand):
    help = "Trusted-operator promotion after exact parity, protocol, approval, and Paper validation evidence"

    def add_arguments(self, parser):
        parser.add_argument("research_id")
        parser.add_argument("implementation_path")
        parser.add_argument("executable_strategy_key")
        parser.add_argument("--implementation-version", default="1")
        parser.add_argument("--actor", required=True)
        parser.add_argument("--evidence-json", required=True)

    def handle(self, *args, **options):
        try:
            strategy = ResearchStrategyDefinition.objects.filter(
                research_id=options["research_id"], dataset_version__status="ACTIVE"
            ).get()
            evidence = json.loads(options["evidence_json"])
            row = promote_strategy(
                strategy,
                implementation_path=options["implementation_path"],
                implementation_version=options["implementation_version"],
                executable_strategy_key=options["executable_strategy_key"],
                approval_actor=options["actor"],
                approval_evidence=evidence,
            )
        except (ResearchStrategyDefinition.DoesNotExist, ResearchStrategyDefinition.MultipleObjectsReturned, ValueError, json.JSONDecodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Promoted implementation {row.pk}; runtime remains governed by PAPER/LIVE controls"))
