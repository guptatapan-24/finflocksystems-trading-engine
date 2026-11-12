from django.core.management.base import BaseCommand, CommandError

from ...models import ResearchExperiment
from ...services.experiment_runner import run_experiment


class Command(BaseCommand):
    help = "Run one pre-budgeted research experiment; it never expands an uncontrolled grid"

    def add_arguments(self, parser):
        parser.add_argument("experiment_id", type=int)

    def handle(self, *args, **options):
        try:
            result = run_experiment(options["experiment_id"])
        except (ResearchExperiment.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(str(result)))
