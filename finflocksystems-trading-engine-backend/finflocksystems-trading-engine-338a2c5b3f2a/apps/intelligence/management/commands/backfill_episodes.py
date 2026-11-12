from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from apps.intelligence.services.episodes import build_or_update_episodes


class Command(BaseCommand):
    """Rebuild trade episodes from historical fills. Idempotent: running it twice changes nothing."""

    help = "Backfill trade episodes. Use --dry-run to count what would be built without writing."

    def add_arguments(self, parser):
        parser.add_argument("--portfolio", type=int, help="Only strategy instances of this portfolio id")
        parser.add_argument("--instance", type=int, help="Only this strategy instance id")
        parser.add_argument("--since", help="Only instances with a fill on/after YYYY-MM-DD")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true", help="Recompute even if nothing changed")

    def handle(self, *args, **options):
        from apps.allocation.models import OrderIntentAttribution
        from apps.strategies.models import StrategyInstance

        instances = StrategyInstance.objects.all()
        if options["portfolio"]:
            instances = instances.filter(portfolio_id=options["portfolio"])
        if options["instance"]:
            instances = instances.filter(pk=options["instance"])
        if options["since"]:
            try:
                since = datetime.strptime(options["since"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD") from exc
            ids = (OrderIntentAttribution.objects.filter(order_intent__order__fills__executed_at__gte=since)
                   .values_list("strategy_instance_id", flat=True))
            instances = instances.filter(pk__in=set(ids))
        built = 0
        for instance in instances.order_by("pk"):
            episodes = build_or_update_episodes(instance.pk, force=options["force"], dry_run=options["dry_run"])
            built += len(episodes)
            self.stdout.write(f"instance {instance.pk} ({instance.name}): {len(episodes)} episode(s)")
        verb = "would build" if options["dry_run"] else "built"
        self.stdout.write(f"Done: {verb} {built} episode(s) across {instances.count()} instance(s)")
