from django.core.management.base import BaseCommand, CommandError

from ...services.bundle_import import BundleImportError, import_bundle
from ...services.bundle_validation import BundleValidationError


class Command(BaseCommand):
    help = "Atomically import a validated research universe bundle"

    def add_arguments(self, parser):
        parser.add_argument("bundle_path")
        parser.add_argument("--activate", action="store_true")
        parser.add_argument("--no-map-instruments", action="store_true")

    def handle(self, *args, **options):
        try:
            dataset, created = import_bundle(
                options["bundle_path"],
                activate=options["activate"],
                map_instruments=not options["no_map_instruments"],
            )
        except (BundleValidationError, BundleImportError) as exc:
            raise CommandError(str(exc)) from exc
        action = "Imported" if created else "Already imported"
        self.stdout.write(self.style.SUCCESS(f"{action} dataset {dataset.pk} ({dataset.status})"))

