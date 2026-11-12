from django.core.management.base import BaseCommand

from ...services.candidate_service import score_completed_trials


class Command(BaseCommand):
    help = "Apply hard rejections and the versioned 0-100 candidate score"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(str(score_completed_trials())))
