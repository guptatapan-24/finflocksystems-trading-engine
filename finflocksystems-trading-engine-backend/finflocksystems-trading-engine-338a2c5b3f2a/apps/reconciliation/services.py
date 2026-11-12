from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import OutboxEvent
from apps.broker_gateway.client import GatewayClient
from apps.execution.models import Fill
from apps.instruments.models import BrokerContract
from apps.portfolios.models import PortfolioPosition

from .models import ReconciliationBreak, ReconciliationRun


def _account_id(row):
    return str(row.get("account") or row.get("account_id") or "")


def _resolve_account(broker_account, broker_account_id, broker_positions, broker_executions):
    if broker_account is not None:
        return broker_account
    if broker_account_id:
        return BrokerAccount.objects.get(account_id=str(broker_account_id))
    snapshot_accounts = {
        _account_id(row)
        for row in [*broker_positions, *broker_executions]
        if _account_id(row)
    }
    if len(snapshot_accounts) == 1:
        return BrokerAccount.objects.get(account_id=snapshot_accounts.pop())
    stored_accounts = list(BrokerAccount.objects.all()[:2])
    if len(stored_accounts) == 1:
        return stored_accounts[0]
    raise ValueError("Reconciliation requires exactly one broker account")


def reconcile(trigger="manual", client=None, *, broker_account=None, broker_account_id=None, gateway_session=None):
    if client is None:
        account_for_route=broker_account or (BrokerAccount.objects.get(account_id=str(broker_account_id)) if broker_account_id else None)
        if account_for_route is None:raise ValueError("Reconciliation requires an explicit session or client")
        mappings=account_for_route.gateway_sessions.filter(available=True).select_related("session")
        if gateway_session is not None:mappings=mappings.filter(session=gateway_session)
        mapping=mappings.first()
        if mapping is None or mappings.count()!=1:raise ValueError("Reconciliation account does not resolve to exactly one gateway session")
        client=GatewayClient(mapping.session)
        gateway_session=mapping.session
    gateway_session=gateway_session or getattr(client,"gateway_session",None)
    # Broker I/O is deliberately completed before the database transaction.
    health = client.health() or {}
    all_broker_positions = client.positions() or []
    all_broker_executions = client.executions() or []
    account = _resolve_account(
        broker_account,
        broker_account_id,
        all_broker_positions,
        all_broker_executions,
    )
    broker_positions_rows = [
        row for row in all_broker_positions if _account_id(row) == account.account_id
    ]
    broker_execution_rows = [
        row for row in all_broker_executions if _account_id(row) == account.account_id
    ]

    with transaction.atomic():
        account = BrokerAccount.objects.select_for_update().get(pk=account.pk)
        run = ReconciliationRun.objects.create(trigger=trigger, broker_account=account,gateway_session=gateway_session)
        if not health.get("connected") or not health.get("reconciled"):
            ReconciliationBreak.objects.create(
                run=run,
                category="GATEWAY",
                severity="CRITICAL",
                internal_value={"account": account.account_id, "expected": "CONNECTED_RECONCILED"},
                broker_value=health,
                material=True,
            )

        broker_positions = defaultdict(Decimal)
        for row in broker_positions_rows:
            broker_positions[str(row.get("conid"))] += Decimal(str(row.get("quantity", 0)))
        internal_positions = defaultdict(Decimal)
        position_query = PortfolioPosition.objects.filter(portfolio__account=account)
        if gateway_session is not None:
            position_query = position_query.filter(portfolio__gateway_session=gateway_session)
        contracts = {
            item.instrument_id: str(item.conid)
            for item in BrokerContract.objects.filter(
                instrument_id__in=position_query.values("instrument_id")
            )
        }
        for row in position_query:
            if row.instrument_id in contracts:
                internal_positions[contracts[row.instrument_id]] += row.quantity
        for conid in sorted(set(broker_positions) | set(internal_positions)):
            if broker_positions[conid] != internal_positions[conid]:
                ReconciliationBreak.objects.create(
                    run=run,
                    category="POSITION",
                    severity="CRITICAL",
                    internal_value={
                        "account": account.account_id,
                        "conid": conid,
                        "quantity": str(internal_positions[conid]),
                    },
                    broker_value={
                        "account": account.account_id,
                        "conid": conid,
                        "quantity": str(broker_positions[conid]),
                    },
                    material=True,
                )

        session_prefix = f"{gateway_session.pk}:" if gateway_session is not None else ""
        broker_execs = {
            f"{session_prefix}{row.get('execution_id')}"
            for row in broker_execution_rows
            if row.get("execution_id")
        }
        fill_query = Fill.objects.filter(order__intent__portfolio__account=account)
        if gateway_session is not None:
            fill_query = fill_query.filter(order__intent__portfolio__gateway_session=gateway_session)
        internal_execs = set(fill_query.values_list("execution_id", flat=True))
        for execution_id in sorted(broker_execs - internal_execs):
            ReconciliationBreak.objects.create(
                run=run,
                category="EXECUTION",
                severity="CRITICAL",
                internal_value={"account": account.account_id, "execution_id": None},
                broker_value={"account": account.account_id, "execution_id": execution_id},
                material=True,
            )
        for execution_id in sorted(internal_execs - broker_execs):
            ReconciliationBreak.objects.create(
                run=run,
                category="EXECUTION",
                severity="WARNING",
                internal_value={"account": account.account_id, "execution_id": execution_id},
                broker_value={"account": account.account_id, "execution_id": None},
                material=False,
            )

        for category in ("GATEWAY", "POSITION", "EXECUTION", "ORDER", "CASH", "ACCOUNT"):
            if not run.breaks.filter(category=category, material=True).exists():
                prior_breaks = ReconciliationBreak.objects.filter(
                    run__broker_account=account,
                    category=category,
                    material=True,
                    resolved=False,
                ).exclude(run=run)
                if gateway_session is not None:
                    prior_breaks = prior_breaks.filter(
                        Q(run__gateway_session=gateway_session)
                        | Q(run__gateway_session__isnull=True)
                    )
                else:
                    prior_breaks = prior_breaks.filter(run__gateway_session__isnull=True)
                prior_breaks.update(
                    resolved=True,
                    resolution=f"Automatically resolved by clean account reconciliation run {run.pk}",
                )

        material = run.breaks.filter(material=True, resolved=False).exists()
        run.status = "BLOCKED" if material else "COMPLETED"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])
        account.is_reconciled = not material
        account.save(update_fields=["is_reconciled", "updated_at"])
        OutboxEvent.objects.create(
            topic="reconciliation.events.v1",
            event_type="reconciliation.completed",
            aggregate_type="account",
            aggregate_id=account.account_id,
            partition_key=account.account_id,
            payload={
                "reconciliation_run_id": run.pk,
                "account": account.account_id,
                "status": run.status,
                "material_breaks": run.breaks.filter(material=True).count(),
            },
            idempotency_key=f"reconciliation:{run.pk}:completed",
        )
        return run
