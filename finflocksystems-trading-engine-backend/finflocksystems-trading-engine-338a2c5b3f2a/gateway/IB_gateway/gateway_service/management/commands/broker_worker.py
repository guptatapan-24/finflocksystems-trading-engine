import fcntl, logging, os, socket, time, uuid
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections
from django.utils import timezone
from broker.factory import create_adapter
from gateway_service.models import GatewayCommand, GatewayEvent, GatewayHealthSnapshot, GatewaySession
from gateway_service.services import (
    claim_next_command,
    compact_gateway_operational_records,
    fail_command,
    persist_event,
    process_command,
    recover_expired_commands,
)

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Run the sole process allowed to own the TWS connection"
    def handle(self,*args,**options):
        lock=open("/tmp/ibkr-broker-owner.lock","w")
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Another broker connection owner is already running")
        owner=f"{socket.gethostname()}:{os.getpid()}"; adapter=create_adapter()
        session,_=GatewaySession.objects.update_or_create(pk=1,defaults={"state":"CONNECTING","mode":settings.IBC_TRADING_MODE,"reconciled":False,"connection_owner":owner})
        backoff=1; last_refresh=0;last_compaction=0;was_connected=False
        def publish_snapshot(state):
            stamp=time.time_ns()
            for key in ("accounts","account_summary","open_orders","completed_orders","executions"):
                persist_event(f"snapshot:{key}:{stamp}",f"snapshot.{key}",{"value":state.get(key,[])})
            positions = state.get("positions", [])
            persist_event(
                f"snapshot:positions-all:{stamp}",
                "snapshot.positions_all",
                {"value": positions, "complete": True, "snapshot_id": str(stamp)},
            )
            account_ids = set()
            for account in state.get("accounts", []):
                account_id = account if isinstance(account, str) else account.get("account_id") or account.get("account")
                if account_id:
                    account_ids.add(str(account_id))
            account_ids.update(str(row["account"]) for row in positions if row.get("account"))
            for account_id in sorted(account_ids):
                account_positions = [row for row in positions if str(row.get("account") or "") == account_id]
                persist_event(
                    f"snapshot:positions:{account_id}:{stamp}",
                    "snapshot.positions",
                    {"value": account_positions, "account": account_id, "complete": True,
                     "snapshot_id": f"{stamp}:{account_id}"},
                )
            GatewaySession.objects.filter(pk=1).update(state="CONNECTED",reconciled=bool(state.get("reconciled")),last_callback_at=timezone.now())
            GatewayHealthSnapshot.objects.create(connected=True,reconciled=bool(state.get("reconciled")),details={"accounts":len(state.get("accounts",[])),"positions":len(state.get("positions",[])),"open_orders":len(state.get("open_orders",[])),"executions":len(state.get("executions",[]))})
        def publish_market_events():
            for payload in adapter.drain_market_events():
                event_type="market.error" if payload.get("event_kind")=="ERROR" else "market.raw"
                persist_event(f"market:{payload['source_event_id']}",event_type,payload)
        def publish_order_events():
            for payload in adapter.drain_order_events():
                persist_event(f"broker-order:{payload['source_event_id']}","broker.order",payload)
        while True:
            close_old_connections()
            try:
                if not adapter.is_connected():
                    adapter.connect(); state=adapter.refresh_state()
                    GatewaySession.objects.filter(pk=1).update(connection_generation=uuid.uuid4())
                    publish_snapshot(state); last_refresh=time.monotonic()
                    persist_event(f"connected:{time.time_ns()}","session.connected",{"reconciled":state.get("reconciled",False)})
                    backoff=1; was_connected=True
                elif time.monotonic()-last_refresh >= settings.BROKER_REFRESH_SECONDS:
                    publish_snapshot(adapter.refresh_state()); last_refresh=time.monotonic()
                recover_expired_commands(adapter)
                if time.monotonic()-last_compaction>=settings.GATEWAY_COMPACTION_SECONDS:
                    compact_gateway_operational_records();last_compaction=time.monotonic()
                command=claim_next_command(owner)
                if command:
                    try: process_command(command,adapter)
                    except Exception as exc:
                        command=fail_command(command,exc)
                        persist_event(f"command:{command.pk}:failed","command.failed",{"command_id":command.pk,"command_type":command.command_type,
                            "payload":command.payload,"error":command.last_error,"retryable":command.retryable})
                else: adapter.wait(0.2)
                publish_market_events()
                publish_order_events()
            except DatabaseError as exc:
                # SQLite contention is not a broker outage; reporting it as one
                # makes the backend fail over every live market-data subscription.
                logger.warning("Gateway database error in broker loop: %s", exc)
                time.sleep(1)
            except Exception as exc:
                GatewaySession.objects.filter(pk=1).update(state="DISCONNECTED",reconciled=False,last_callback_at=timezone.now())
                GatewayHealthSnapshot.objects.create(connected=False,reconciled=False,details={"error":str(exc)[:500]})
                # Only a lost connection is a disconnect; failed attempts while
                # IB Gateway is still logging in never had one to lose.
                if was_connected:
                    persist_event(f"disconnected:{time.time_ns()}","session.disconnected",
                        {"error":str(exc)[:500],"occurred_at":timezone.now().isoformat()})
                was_connected=False
                time.sleep(min(backoff,30)); backoff=min(backoff*2,30)
