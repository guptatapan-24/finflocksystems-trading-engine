from django.core.management.base import CommandError
from django.core.management.commands import flush as core_flush

from apps.intelligence.guard import OVERRIDE_FLAG, flush_refusal_message


class Command(core_flush.Command):
    """Django's flush, but it refuses while intelligence data exists (R6)."""

    help = core_flush.Command.help + " Refuses if intel_ tables hold rows."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(OVERRIDE_FLAG, action="store_true", dest="intel_override", default=False,
                            help="Flush even though intelligence data exists.")

    def handle(self, *args, **options):
        if not options.pop("intel_override", False):
            message = flush_refusal_message()
            if message:
                raise CommandError(message)
        return super().handle(*args, **options)
