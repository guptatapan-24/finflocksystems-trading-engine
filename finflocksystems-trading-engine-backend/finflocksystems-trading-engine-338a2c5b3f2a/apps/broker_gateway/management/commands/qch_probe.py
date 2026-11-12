"""Find out, safely, what the platform's sub-container API (QCH) will accept. Used before moving any service into a sub-container.

    python manage.py qch_probe list
    python manage.py qch_probe create --image trading_engine_20:v4 --command "sleep 120" --yes
    python manage.py qch_probe delete --name probe-sleep --yes

It only creates containers whose name starts with `probe-`, never passes any environment variable (so no secret can leak into a probe), and
prints container names and statuses only. Nothing here touches the broker sessions or the trading flow.
"""
import re
import shlex

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.broker_gateway.qch import QCHBrokerClient, QCHError

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/@:-]{0,254}$")
NAME_RE = re.compile(r"^probe-[a-z0-9][a-z0-9_.-]{0,100}$")


class Command(BaseCommand):
    help = "Probe the QCH sub-container API: list, create a harmless probe-* container, delete it. Prints no secrets."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "create", "delete"])
        parser.add_argument("--image", help="image to start (e.g. the app's own image, or docker.io/<user>/<repo>:<tag>)")
        parser.add_argument("--command", default="sleep 120", help="command to run in the probe container (default: sleep 120)")
        parser.add_argument("--name", default="probe-sleep")
        parser.add_argument("--command-as-string", action="store_true", help="send the command as one string instead of a list")
        parser.add_argument("--yes", action="store_true", help="required for create and delete")

    def handle(self, *args, **options):
        try:
            client = QCHBrokerClient()
            action = options["action"]
            if action == "list":
                containers = client.list_containers()
                self.stdout.write(f"{len(containers)} sub-container(s)")
                for item in containers:
                    self.stdout.write(f"  {item.name}  {item.status}")
                return
            name = options["name"]
            if not NAME_RE.fullmatch(name):
                raise CommandError("--name must start with 'probe-' (lowercase letters, digits, - _ .)")
            if not options["yes"]:
                raise CommandError(f"{action} needs --yes")
            if action == "delete":
                self.stdout.write("deleted" if client.delete_container(name) else "not found")
                return
            image = str(options["image"] or "").strip()
            if not IMAGE_RE.fullmatch(image):
                raise CommandError("--image is required and must look like an image reference")
            words = shlex.split(options["command"])
            command = options["command"] if options["command_as_string"] else words
            network = str(getattr(settings, "QCH_SUBCONTAINER_NETWORK", "") or "").strip() or None
            created = client.create_container(name=name, image=image, command=command, env={}, network=network)
            self.stdout.write(f"created {created.name} status={created.status}")
            self.stdout.write("check it with `qch_probe list`, then remove it with `qch_probe delete --name %s --yes`" % name)
        except QCHError as exc:
            raise CommandError(f"QCH said: {exc} (http {exc.status_code})") from None
