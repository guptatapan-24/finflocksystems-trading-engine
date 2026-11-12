import json

from django.core.management.base import BaseCommand, CommandError

from ...models import ResearchUniverse
from ...services.universe_mapping import map_research_universe


class Command(BaseCommand):
    help = "Deterministically map research members without making broker calls"

    def add_arguments(self, parser):
        parser.add_argument("--universe-id", type=int)
        parser.add_argument("--no-create", action="store_true")

    def handle(self, *args, **options):
        query = ResearchUniverse.objects.filter(active=True)
        if options["universe_id"]:
            query = query.filter(pk=options["universe_id"])
        universe = query.order_by("-dataset_version__snapshot_date").first()
        if not universe:
            raise CommandError("No matching active research universe")
        result = map_research_universe(universe, create_unqualified=not options["no_create"])
        self.stdout.write(json.dumps(result, sort_keys=True))

