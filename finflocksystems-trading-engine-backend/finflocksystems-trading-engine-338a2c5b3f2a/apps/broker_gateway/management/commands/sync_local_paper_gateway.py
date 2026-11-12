from django.core.management.base import BaseCommand, CommandError

from apps.broker_gateway.services import synchronize_local_paper_gateway
from apps.reconciliation.services import reconcile


class Command(BaseCommand):
    help = "Synchronize the Docker Compose paper Gateway and optionally reconcile its accounts."

    def add_arguments(self, parser):
        parser.add_argument("--reconcile", action="store_true")

    def handle(self, *args, **options):
        session = synchronize_local_paper_gateway()
        if session is None:
            raise CommandError("Local paper Gateway environment is not configured")
        session.refresh_from_db()
        if session.status != session.Status.CONNECTED or not session.commands_enabled:
            raise CommandError("Local paper Gateway is not connected and command-ready")
        runs = []
        if options["reconcile"]:
            for mapping in session.session_accounts.filter(available=True).select_related(
                "broker_account"
            ):
                run = reconcile(
                    trigger="local-paper-bootstrap",
                    broker_account=mapping.broker_account,
                    gateway_session=session,
                )
                if run.status != "COMPLETED":
                    raise CommandError(
                        f"Paper account reconciliation {run.pk} is blocked"
                    )
                runs.append(run.pk)
        self.stdout.write(
            self.style.SUCCESS(
                f"Local paper Gateway synchronized; session={session.pk}; "
                f"reconciliations={len(runs)}"
            )
        )
