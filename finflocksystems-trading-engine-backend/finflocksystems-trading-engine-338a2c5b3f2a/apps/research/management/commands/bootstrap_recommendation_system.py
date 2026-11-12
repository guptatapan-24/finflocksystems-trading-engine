import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.broker_gateway.client import GatewayClient, GatewayError
from apps.broker_gateway.models import BrokerGatewaySession
from apps.market_data.mapping import verify_finnhub_mapping
from apps.portfolio_construction.models import StrategyConstructionProfile

from ...models import BacktestProtocolVersion, ResearchStrategyImplementation
from ...services.bundle_import import BundleImportError, import_bundle
from ...services.bundle_validation import BundleValidationError
from ...services.recommendation_cache import prepare_recommendation_caches
from ...services.strategy_registry import synchronize_strategy_registry
from ...services.universe_pipeline import (
    active_recommendation_universe,
    map_universe_batch,
    qualify_universe_batch,
)


class Command(BaseCommand):
    help = "Idempotently install and prepare the complete local 500-stock/97-strategy recommendation system"

    def add_arguments(self, parser):
        parser.add_argument("--bundle-path", default=settings.RESEARCH_BUNDLE_PATH)
        parser.add_argument("--batch-size", type=int, default=25)
        parser.add_argument("--broker-session-id")
        parser.add_argument(
            "--skip-external",
            action="store_true",
            help="Explicitly skip IBKR qualification and Finnhub verification",
        )

    def _gateway(self, options):
        if options["skip_external"]:
            return None
        session_id = str(options.get("broker_session_id") or "").strip()
        if not session_id:
            raise CommandError("--broker-session-id is required unless --skip-external is explicitly set")
        try:
            session = BrokerGatewaySession.objects.get(pk=session_id)
            return GatewayClient(session, require_commands=True)
        except (BrokerGatewaySession.DoesNotExist, ValueError, GatewayError) as exc:
            raise CommandError(f"Broker session is not available for qualification: {exc}") from exc

    def handle(self, *args, **options):
        gateway = self._gateway(options)
        batch_size = max(1, min(100, options["batch_size"]))
        try:
            dataset, imported = import_bundle(
                options["bundle_path"],
                activate=True,
                map_instruments=True,
            )
            universe = active_recommendation_universe(require_complete=True)
            if universe.dataset_version_id != dataset.pk:
                raise ValueError("Imported dataset is not the active recommendation universe")

            protocol = dataset.protocols.order_by("pk").first()
            if not protocol:
                raise ValueError("Imported research bundle did not create a backtest protocol")
            BacktestProtocolVersion.objects.filter(dataset_version=dataset).exclude(pk=protocol.pk).update(active=False)
            if not protocol.active:
                protocol.active = True
                protocol.save(update_fields=["active", "updated_at"])

            registry = synchronize_strategy_registry(dataset)
            member_count = universe.members.filter(active=True, membership_end__isnull=True).count()
            mapped = 0
            mapping_failures = []
            for offset in range(0, member_count, batch_size):
                result = map_universe_batch(offset=offset, batch_size=batch_size)
                mapped += result["processed"]
                mapping_failures.extend(result["failed"])
            if mapping_failures:
                raise ValueError(
                    f"Canonical instrument mapping failed for {len(mapping_failures)} universe members; "
                    f"first failure: {mapping_failures[0]}"
                )

            qualified = verified = 0
            qualification_failures = []
            finnhub_failures = []
            if gateway is not None:
                for offset in range(0, member_count, batch_size):
                    result = qualify_universe_batch(
                        offset=offset,
                        batch_size=batch_size,
                        gateway=gateway,
                    )
                    qualified += result["qualified"]
                    qualification_failures.extend(result["failed"])
                if qualified == 0 and qualification_failures:
                    report = {
                        "ibkr_qualified": qualified,
                        "qualification_failures": len(qualification_failures),
                        "first_failure": qualification_failures[0],
                    }
                    raise CommandError(
                        "IBKR qualification failed for every attempted member; bootstrap is not successful: "
                        + json.dumps(report, sort_keys=True)
                    )
                for member in universe.members.filter(
                    active=True, instrument__isnull=False
                ).select_related("instrument__broker_contract"):
                    if not hasattr(member.instrument, "broker_contract"):
                        continue
                    try:
                        mapping = verify_finnhub_mapping(member.instrument)
                        verified += int(mapping.status == "VERIFIED")
                        if mapping.status != "VERIFIED":
                            finnhub_failures.append({
                                "symbol": member.source_symbol,
                                "stage": "FINNHUB",
                                "error": mapping.last_error,
                            })
                    except Exception as exc:
                        finnhub_failures.append({
                            "symbol": member.source_symbol,
                            "stage": "FINNHUB",
                            "error": str(exc)[:500],
                        })

            cache_report = prepare_recommendation_caches()
            runtime_mappings = ResearchStrategyImplementation.objects.filter(
                research_strategy__dataset_version=dataset,
                executable_strategy_definition__isnull=False,
            )
            report = {
                "bundle_path": str(options["bundle_path"]),
                "dataset_id": dataset.pk,
                "dataset_status": dataset.status,
                "imported": imported,
                "universe": universe.key,
                "active_members": member_count,
                "mapped_processed": mapped,
                "strategies_registered": registry["registered"],
                "runtime_mappings": runtime_mappings.count(),
                "construction_profiles": StrategyConstructionProfile.objects.filter(
                    strategy_definition_id__in=runtime_mappings.values("executable_strategy_definition_id"),
                    construction_enabled=True,
                ).count(),
                "active_protocol": protocol.protocol_id,
                "external": {
                    "status": "SKIPPED_EXPLICITLY" if gateway is None else "ATTEMPTED",
                    "broker_session_id": str(options.get("broker_session_id") or "") or None,
                    "ibkr_qualified": qualified,
                    "ibkr_failures": len(qualification_failures),
                    "finnhub_verified": verified,
                    "finnhub_failures": len(finnhub_failures),
                },
                "recommendation_caches": cache_report,
                "safety": {
                    "live_trading_changed": False,
                    "orders_created": False,
                    "strategy_instances_enabled": False,
                    "demo_research_data_created": False,
                },
            }
        except CommandError:
            raise
        except (BundleValidationError, BundleImportError, ValueError, GatewayError) as exc:
            raise CommandError(str(exc)) from exc

        serialized = json.dumps(report, sort_keys=True, default=str)
        if gateway is None or cache_report["failures"] or qualification_failures or finnhub_failures:
            self.stdout.write(self.style.WARNING(
                "Bootstrap configuration completed with explicit readiness blockers; no production research data was fabricated."
            ))
            self.stdout.write(serialized)
        else:
            self.stdout.write(self.style.SUCCESS(serialized))
