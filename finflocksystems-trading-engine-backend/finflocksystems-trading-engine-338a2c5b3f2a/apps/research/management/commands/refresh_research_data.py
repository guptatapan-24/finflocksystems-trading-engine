from django.core.management.base import BaseCommand, CommandError

from ...models import ResearchUniverse
from ...services.research_data import stage_operational_history


class Command(BaseCommand):
    help = "Stage existing provider history without claiming split/dividend/total-return research readiness"

    def add_arguments(self, parser):
        parser.add_argument("--universe-id", type=int)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        query = ResearchUniverse.objects.filter(active=True)
        if options["universe_id"]:
            query = query.filter(pk=options["universe_id"])
        universe = query.order_by("-dataset_version__snapshot_date").first()
        if not universe:
            raise CommandError("No matching active research universe")
        total = 0
        members = universe.members.filter(active=True, instrument__isnull=False)[:max(1, min(options["limit"], 500))]
        for member in members:
            total += stage_operational_history(member.instrument)
        self.stdout.write(self.style.WARNING(
            f"Staged {total} SUSPECT bars; corporate-action reconciliation is required before VALID status"
        ))
