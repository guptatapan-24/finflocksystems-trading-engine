import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from ...models import ResearchUniverse
from ...services.eligibility import calculate_universe_eligibility


class Command(BaseCommand):
    help = "Calculate independent research and Portfolio Builder eligibility snapshots"

    def add_arguments(self, parser):
        parser.add_argument("--universe-id", type=int)
        parser.add_argument("--as-of")

    def handle(self, *args, **options):
        query = ResearchUniverse.objects.filter(active=True)
        if options["universe_id"]:
            query = query.filter(pk=options["universe_id"])
        universe = query.order_by("-dataset_version__snapshot_date").first()
        if not universe:
            raise CommandError("No matching active research universe")
        as_of = date.fromisoformat(options["as_of"]) if options["as_of"] else None
        self.stdout.write(json.dumps(calculate_universe_eligibility(universe, as_of_date=as_of), sort_keys=True))
