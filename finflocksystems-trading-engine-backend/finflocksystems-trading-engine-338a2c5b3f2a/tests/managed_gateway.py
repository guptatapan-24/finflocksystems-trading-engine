from apps.broker_gateway.crypto import encrypt_secret
from apps.broker_gateway.models import BrokerGatewaySession, BrokerSessionAccount
from apps.broker_gateway.services import container_name_for


def bind_gateway_mode(portfolio, *, mode="paper"):
    """Bind a portfolio to a mode-matched ready session without external I/O."""
    session = BrokerGatewaySession(
        display_name=f"Test {portfolio.name}",
        username_hint="test",
        mode=mode,
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="pending",
        encrypted_gateway_token=encrypt_secret("gateway-mode-test-token"),
        encrypted_novnc_password=encrypt_secret("gateway-mode-test-vnc"),
        commands_enabled=True,
        last_gateway_state={"connected": True, "reconciled": True, "mode": mode},
    )
    session.child_container_name = container_name_for(session.pk)
    session.internal_base_url = f"http://{session.child_container_name}:8080/api/v1"
    session.save()
    portfolio.account.is_reconciled = True
    portfolio.account.save(update_fields=["is_reconciled", "updated_at"])
    BrokerSessionAccount.objects.create(
        session=session,
        broker_account=portfolio.account,
        available=True,
    )
    portfolio.gateway_session = session
    portfolio.save(update_fields=["gateway_session"])
    return session


def bind_managed_gateway(portfolio, settings, *, mode="paper"):
    """Bind a portfolio to a realistic connected session for broker-facing tests."""
    settings.BROKER_SESSION_ENCRYPTION_KEY = "managed-gateway-fixture-encryption-key"
    session = BrokerGatewaySession(
        display_name=f"Test {portfolio.name}",
        username_hint="te\u2022\u2022er",
        mode=mode,
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="pending",
        encrypted_gateway_token=encrypt_secret("managed-gateway-fixture-token"),
        encrypted_novnc_password=encrypt_secret("vnc-test"),
        commands_enabled=True,
        last_gateway_state={"connected": True, "reconciled": True, "mode": mode},
    )
    session.child_container_name = container_name_for(session.pk)
    session.internal_base_url = f"http://{session.child_container_name}:8080/api/v1"
    session.save()
    BrokerSessionAccount.objects.create(
        session=session,
        broker_account=portfolio.account,
        available=True,
    )
    portfolio.gateway_session = session
    portfolio.save(update_fields=["gateway_session"])
    return session
