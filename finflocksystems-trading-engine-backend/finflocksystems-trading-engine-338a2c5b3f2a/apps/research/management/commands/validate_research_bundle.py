import json

from django.core.management.base import BaseCommand, CommandError

from ...services.bundle_validation import BundleValidationError, validate_bundle


class Command(BaseCommand):
    help = "Validate research bundle schemas, hashes, counts, enums, and relationships"

    def add_arguments(self, parser):
        parser.add_argument("bundle_path")

    def handle(self, *args, **options):
        try:
            validated = validate_bundle(options["bundle_path"])
        except BundleValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(validated.report, sort_keys=True))
        self.stdout.write(self.style.SUCCESS("Research bundle is valid"))

