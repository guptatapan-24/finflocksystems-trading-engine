from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone

from apps.audit.models import OutboxEvent
from apps.event_bus.models import StreamHealthMetric
from apps.market_streams.models import MarketBar, MarketDataSubscription
from apps.market_streams.services import _warmup_bars, current_warmup_required
from apps.strategies.models import StrategyInstance

# The consumer writes its heartbeat at least every 10 s while it is polling.
CONSUMER_STALE_SECONDS = 60


def _age(moment, now):
    return "never" if moment is None else f"{int((now - moment).total_seconds())}s ago"


class Command(BaseCommand):
    help = (
        "Read-only. Explains why a strategy is stuck warming up or blocked with 'Warm-up timeout': "
        "it walks the same chain the timeout check walks and says which link is broken. Writes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("instance_id", nargs="?", type=int, help="strategy instance id (default: every enabled or blocked one)")

    def handle(self, *args, **options):
        now = timezone.now()
        out = self.stdout.write
        out("== Pipeline processes (heartbeats) ==")
        consumer_ok = False
        for metric in StreamHealthMetric.objects.order_by("component", "metric"):
            age = (now - metric.observed_at).total_seconds()
            out(f"  {metric.component}/{metric.metric}: {metric.status}, updated {int(age)}s ago")
            if metric.component == "backend-market-consumer" and metric.metric == "heartbeat":
                consumer_ok = age <= CONSUMER_STALE_SECONDS and metric.status != "STOPPED"
        if not consumer_ok:
            out(self.style.ERROR(
                f"  !! The market consumer has no fresh heartbeat (>{CONSUMER_STALE_SECONDS}s or missing). "
                "Bars are not being stored, so warm-up cannot advance. Check the 'market-consumer' lines in the "
                "container log and `supervisorctl status`."))
        failed = OutboxEvent.objects.filter(status="FAILED", topic__in=["strategy.inputs.v1", "market.raw.v1"]).order_by("-created_at")[:3]
        for event in failed:
            out(self.style.ERROR(f"  !! Failed Kafka publication on {event.topic}: {event.last_error[:160]}"))

        query = StrategyInstance.objects.select_related("instrument", "portfolio__gateway_session")
        if options["instance_id"]:
            query = query.filter(pk=options["instance_id"])
        else:
            query = query.filter(enabled=True) | query.filter(state="BLOCKED")
        for instance in query.order_by("pk"):
            self._instance(instance, now)

    def _instance(self, instance, now):
        out = self.stdout.write
        required = current_warmup_required(instance)
        subscription = MarketDataSubscription.objects.filter(
            gateway_session=instance.portfolio.gateway_session, instrument=instance.instrument, timeframe=instance.timeframe,
        ).first()
        bars = _warmup_bars(instance, subscription, required) if required else []
        out("")
        out(f"== #{instance.pk} {instance.name} · {instance.instrument.symbol} {instance.timeframe} · state {instance.state} ==")
        if instance.block_reason:
            out(f"  block reason: {instance.block_reason}")
        out(f"  warm-up: {min(len(bars), required)}/{required} usable bars (stored progress {instance.warmup_progress}); "
            f"last progress {_age(instance.warmup_last_progress_at, now)}, started {_age(instance.warmup_started_at, now)}")
        if subscription is None:
            out(self.style.ERROR("  !! no market-data subscription exists for this session, instrument and timeframe"))
        else:
            out(f"  subscription: {subscription.state} via {subscription.active_provider}, last event {_age(subscription.last_event_at, now)}, "
                f"requested {_age(subscription.requested_at, now)}")
            if subscription.last_error:
                out(self.style.ERROR(f"  !! subscription error: {subscription.last_error[:200]}"))
        rows = (MarketBar.objects.filter(instrument=instance.instrument, interval=instance.timeframe)
                .values("processing_mode", "is_final").annotate(n=Count("id"), newest=Max("window_end"), received=Max("received_at"))
                .order_by("processing_mode", "is_final"))
        out("  stored bars for this instrument and timeframe:")
        if not rows:
            out(self.style.ERROR("    none: nothing has ever been persisted (provider events, Flink or the consumer)"))
        for row in rows:
            out(f"    {row['processing_mode']:7} final={row['is_final']!s:5} count={row['n']:5} newest bar {row['newest']} received {_age(row['received'], now)}")
        out(self._verdict(instance, subscription, required, bars, now))

    @staticmethod
    def _verdict(instance, subscription, required, bars, now):
        if subscription is None:
            return "  VERDICT: no subscription was created. Retry activation and check the broker-command log."
        if subscription.state in {"PENDING", "SUBSCRIBING"}:
            return "  VERDICT: the subscribe command to the gateway is still pending or failed to complete."
        if subscription.state == "ERROR":
            return "  VERDICT: the subscription failed (see error above); fix the gateway or market-data access, then retry."
        if subscription.last_event_at is None:
            return "  VERDICT: subscribed but the gateway has never delivered an event; check IBKR market-data permissions and the gateway."
        if len(bars) >= required:
            return "  VERDICT: enough warm-up bars are stored. Retry activation; the next refresh should release the strategy."
        newest = MarketBar.objects.filter(instrument=instance.instrument, interval=instance.timeframe, processing_mode="WARMUP",
                                          is_final=True).aggregate(m=Max("received_at"))["m"]
        if newest is None or newest < (subscription.requested_at or now) - timedelta(hours=1):
            return ("  VERDICT: no WARM-UP history has arrived for this subscription. Bars come gateway -> Kafka -> Flink -> consumer: "
                    "check the Flink job is RUNNING, the consumer heartbeat above, and the gateway's historical-data response.")
        return "  VERDICT: history is arriving but is not enough yet. It is slow or partial; allow more time or raise WARMUP_TIMEOUT_SECONDS."
