from django.core.management.base import BaseCommand

from nfo_ingestion.scheduler import Scheduler


class Command(BaseCommand):
    help = ("Long-running scheduler for the nightly NFO ingestion (supervisor program nfo-ingest-scheduler). Idles unless "
            "NFO_INGEST_SCHEDULE_ENABLED=true. Starts the worker as a separate low-priority process, Mon-Fri from 22:00 IST.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Evaluate the schedule once and exit.")

    def handle(self, *args, **options):
        Scheduler(log=self.stdout.write).loop(once=options["once"])
