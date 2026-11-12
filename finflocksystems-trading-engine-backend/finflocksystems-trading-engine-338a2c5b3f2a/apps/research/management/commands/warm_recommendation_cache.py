from django.core.management.base import BaseCommand, CommandError

from ...services.recommendation_cache import warm_all_recommendation_caches


class Command(BaseCommand):
    help = "Precompute recommendation snapshots for every valid timeframe/risk profile"

    def handle(self, *args, **options):
        try:
            result = warm_all_recommendation_caches()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(str(result)))

