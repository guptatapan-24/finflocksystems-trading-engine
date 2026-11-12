from django.core.management.base import BaseCommand, CommandError

from nfo_ingestion.scheduler import RunState, run_worker


class Command(BaseCommand):
    help = ("Run the NFO ingestion by hand, with all the guardrails (idle window 16:30-08:30 IST only, one session at a time). "
            "--dry-run reports only: no download, no database write, no gateway contact. --session D ingests exactly that session; "
            "re-running a finished session is a no-op (the ledger skips it). Refuses to start while the scheduled run is active.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--session", help="YYYY-MM-DD")
        parser.add_argument("--max-sessions", type=int, default=0)

    def handle(self, *args, **options):
        extra = []
        if options["dry_run"]:
            extra.append("--dry-run")
        if options["session"]:
            extra += ["--session", options["session"]]
        if options["max_sessions"]:
            extra += ["--max-sessions", str(options["max_sessions"])]
        state = RunState()
        token = state.acquire(6 * 3600)
        if token is None:
            raise CommandError("An ingestion run is already active; try again when it has finished.")
        try:
            summary = run_worker(extra, schedule_enabled=False, log=self.stdout.write)
        finally:
            state.release(token)
        if summary.get("exit_code") not in (0, None):
            raise CommandError(f"Ingestion ended with exit code {summary['exit_code']} ({summary.get('code')}): {summary.get('message', '')}")
