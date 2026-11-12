import signal
import threading

from django.core.management.base import BaseCommand

from apps.core.db_outage import DbOutageBackoff
from apps.market_data.realtime import FinnhubRealtimeWorker


class Command(BaseCommand):
    help = "Consume Finnhub WebSocket trades and publish UTC-aligned 5-second fallback bars"

    def handle(self, *args, **options):
        stop_event = threading.Event()

        def stop(*_args):
            stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # A database outage is waited out here with bounded backoff instead of
        # exiting for supervisord to respawn. Each attempt builds a new worker, so
        # state is exactly what a restarted process would start with.
        DbOutageBackoff("finnhub-market-data", stop=stop_event.is_set).run(
            lambda: FinnhubRealtimeWorker(stop_event=stop_event).run_forever()
        )
