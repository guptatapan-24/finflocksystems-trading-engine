from django.apps import apps
from django.core.management.base import BaseCommand

from apps.strategies.seeding import seed_option_strategy_definitions


class Command(BaseCommand):
    help = "Idempotently seed every built-in stock and option strategy definition."

    def handle(self, *args, **options):
        # The stock definitions are owned by the original bootstrap migration;
        # this command is also used after the deployment's intentional flush.
        from importlib import import_module

        import_module(
            "apps.strategies.migrations.0002_orderpolicy_strategydefinition_strategyriskpolicy_and_more"
        ).seed_plugins(apps, None)
        seed_option_strategy_definitions(apps.get_model("strategies", "StrategyDefinition"))
        self.stdout.write(self.style.SUCCESS("All built-in strategy definitions are seeded."))
