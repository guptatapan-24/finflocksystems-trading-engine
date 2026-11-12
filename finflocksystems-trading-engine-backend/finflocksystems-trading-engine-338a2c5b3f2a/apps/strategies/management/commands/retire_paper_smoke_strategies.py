from django.core.management.base import BaseCommand

from apps.strategies.models import StrategyInstance


class Command(BaseCommand):
    help = "Retire strategy fixtures created by the Docker paper-pipeline smoke test."

    def handle(self, *args, **options):
        retired = StrategyInstance.objects.filter(
            name__startswith="docker-paper-sma-"
        ).update(
            enabled=False,
            state="DISABLED",
            kill_switch=True,
            block_reason="Prior Docker paper integration run retired",
        )
        self.stdout.write(f"Retired prior smoke strategies: {retired}")
