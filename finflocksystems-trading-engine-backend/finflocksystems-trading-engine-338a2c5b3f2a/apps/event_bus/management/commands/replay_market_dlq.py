from django.core.management.base import BaseCommand,CommandError

from apps.event_bus.models import DeadLetterEvent
from apps.event_bus.services import replay_dead_letter


class Command(BaseCommand):
    help=(
        "Replay persisted Backend market DLQ rows in REPLAY mode. "
        "Successful rows are marked replayed only after PostgreSQL commits."
    )

    def add_arguments(self,parser):
        parser.add_argument("--id",type=int,dest="dead_letter_id")
        parser.add_argument("--all",action="store_true",dest="replay_all")
        parser.add_argument("--limit",type=int,default=100)

    def handle(self,*args,**options):
        dead_letter_id=options["dead_letter_id"]
        if bool(dead_letter_id)==bool(options["replay_all"]):
            raise CommandError("Specify exactly one of --id or --all")
        query=DeadLetterEvent.objects.filter(replayed_at__isnull=True).order_by(
            "created_at","pk"
        )
        if dead_letter_id:
            query=query.filter(pk=dead_letter_id)
        else:
            query=query[:max(1,options["limit"])]
        rows=list(query)
        if dead_letter_id and not rows:
            raise CommandError("Unreplayed dead-letter row was not found")
        replayed=0
        for row in rows:
            replay_dead_letter(row)
            replayed+=1
        self.stdout.write(self.style.SUCCESS(f"Replayed {replayed} market DLQ event(s)"))
