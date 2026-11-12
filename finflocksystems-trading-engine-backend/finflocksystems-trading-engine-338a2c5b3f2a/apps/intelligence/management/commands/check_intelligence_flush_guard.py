from django.core.management.base import BaseCommand, CommandError

from apps.intelligence.guard import flush_refusal_message


class Command(BaseCommand):
    """Exit non-zero if a database flush would destroy intelligence data. Used by entrypoint.sh before any
    destructive startup step (Flink cancel, Kafka flush), so a refusal leaves the system untouched."""

    help = "Fail when intelligence tables contain rows (pre-check for CLEANUP_DATABASE_ON_START)."

    def handle(self, *args, **options):
        message = flush_refusal_message()
        if message:
            raise CommandError(message)
        self.stdout.write("Intelligence flush guard: no intelligence data, cleanup may proceed.")
