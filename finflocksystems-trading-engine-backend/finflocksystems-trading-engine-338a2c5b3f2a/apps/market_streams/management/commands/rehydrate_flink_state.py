import json

from django.core.management.base import BaseCommand

from apps.market_streams.rehydration import (
    rehydrate_flink_state,
    rehydrate_flink_state_if_needed,
)


class Command(BaseCommand):
    help = (
        "Re-feed Flink's bounded state from PostgreSQL through the transactional outbox: "
        "a strategy.inputs.snapshot per (instrument, timeframe) and recent bar history "
        "to market.bars.seed.v1. Without --force it only acts when a required Flink job "
        "is running without restored state."
    )

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
            help="Publish now, without checking the Flink job.")
        parser.add_argument("--instrument-id", type=int, default=None,
            help="Limit the bar-history seed to one instrument (with --force).")
        parser.add_argument("--timeframe", default=None,
            help="Limit the bar-history seed to one timeframe (with --force).")
        parser.add_argument("--snapshot-only", action="store_true",
            help="Publish only the requirement registry snapshot (with --force).")
        parser.add_argument("--seed-only", action="store_true",
            help="Publish only the bar-history seed (with --force).")

    def handle(self, *args, **options):
        if options["snapshot_only"] and options["seed_only"]:
            raise SystemExit("--snapshot-only and --seed-only are mutually exclusive")
        if options["force"]:
            result = rehydrate_flink_state(
                reason="MANUAL",
                instrument_id=options["instrument_id"],
                timeframe=options["timeframe"],
                snapshot=not options["seed_only"],
                seed=not options["snapshot_only"],
            )
        else:
            result = rehydrate_flink_state_if_needed()
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
