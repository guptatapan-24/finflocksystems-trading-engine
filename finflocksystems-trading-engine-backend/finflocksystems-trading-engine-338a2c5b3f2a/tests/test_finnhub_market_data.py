import json
from datetime import date

import pytest
import responses
from django.test import Client, override_settings

from apps.instruments.models import Instrument
from apps.market_data.models import InstrumentPriceHistory, MarketDataFetchRun, MarketDataProviderConfiguration
from apps.market_data.services import (
    FinnhubClient,
    FinnhubError,
    decrypt_api_key,
    effective_api_key,
    encrypt_api_key,
    fetch_daily_history,
    provider_status,
)


pytestmark = pytest.mark.django_db


@override_settings(FINNHUB_ENCRYPTION_KEY="unit-test-encryption-secret", FINNHUB_API_KEY="env-secret")
def test_key_encryption_masking_and_environment_precedence():
    encrypted = encrypt_api_key("database-secret")
    assert encrypted != "database-secret"
    assert decrypt_api_key(encrypted) == "database-secret"
    MarketDataProviderConfiguration.objects.create(
        encrypted_api_key=encrypted,
        api_key_last_four="cret",
        override_environment=True,
    )
    key, source, _ = effective_api_key()
    assert (key, source) == ("env-secret", "ENVIRONMENT")
    status = provider_status()
    assert status["masked_api_key"] == "••••cret"
    assert "env-secret" not in json.dumps(status, default=str)
    assert "database-secret" not in json.dumps(status, default=str)


@override_settings(
    FINNHUB_ENCRYPTION_KEY="unit-test-encryption-secret",
    FINNHUB_API_KEY="env-secret",
    FINNHUB_API_KEY_OVERRIDE_ENABLED=True,
)
def test_explicit_database_override_requires_server_permission():
    MarketDataProviderConfiguration.objects.create(
        encrypted_api_key=encrypt_api_key("database-secret"),
        api_key_last_four="cret",
        override_environment=True,
    )
    key, source, _ = effective_api_key()
    assert (key, source) == ("database-secret", "DATABASE")


@responses.activate
@override_settings(FINNHUB_API_KEY="test-key", FINNHUB_MAX_RETRIES=1)
def test_finnhub_client_uses_secret_header_and_handles_rate_limit_retry():
    responses.add(responses.GET, "https://finnhub.io/api/v1/quote", status=429, json={"error": "limit"})
    responses.add(responses.GET, "https://finnhub.io/api/v1/quote", status=200, json={"c": 123.45})
    result = FinnhubClient().test_connection("AAPL")
    assert result["connected"] is True
    assert len(responses.calls) == 2
    assert responses.calls[-1].request.headers["X-Finnhub-Token"] == "test-key"
    assert "test-key" not in responses.calls[-1].request.url


@override_settings(FINNHUB_ENCRYPTION_KEY="unit-test-encryption-secret", FINNHUB_API_KEY="")
def test_finnhub_configuration_endpoint_encrypts_masks_and_never_returns_secret(client):
    saved = client.post(
        "/api/v1/data-providers/finnhub/configure/",
        data=json.dumps({"api_key": "browser-submitted-secret", "enabled": True}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="configure-finnhub",
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["data"]["masked_api_key"] == "••••cret"
    assert "browser-submitted-secret" not in saved.content.decode()
    stored = MarketDataProviderConfiguration.objects.get()
    assert stored.encrypted_api_key != "browser-submitted-secret"
    assert decrypt_api_key(stored.encrypted_api_key) == "browser-submitted-secret"
    assert stored.api_key_last_four == "cret"


@override_settings(FINNHUB_ENCRYPTION_KEY="unit-test-encryption-secret", FINNHUB_API_KEY="")
def test_csrf_is_required_without_an_administrator_session():
    browser = Client(enforce_csrf_checks=True)
    browser.get("/api/v1/system/")
    token = browser.cookies["csrftoken"].value
    blocked = browser.post(
        "/api/v1/data-providers/finnhub/configure/",
        data=json.dumps({"api_key": "csrf-protected-secret"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="csrf-blocked",
    )
    assert blocked.status_code == 403
    saved = browser.post(
        "/api/v1/data-providers/finnhub/configure/",
        data=json.dumps({"api_key": "csrf-protected-secret"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_IDEMPOTENCY_KEY="csrf-configure",
    )
    assert saved.status_code == 200


@responses.activate
@override_settings(FINNHUB_ENCRYPTION_KEY="unit-test-encryption-secret", FINNHUB_API_KEY="")
def test_transient_finnhub_key_test_does_not_save_the_key(client):
    responses.add(responses.GET, "https://finnhub.io/api/v1/quote", status=200, json={"c": 123.45})
    tested = client.post(
        "/api/v1/data-providers/finnhub/test/",
        data=json.dumps({"api_key": "transient-browser-secret", "symbol": "AAPL"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="transient-test",
    )
    assert tested.status_code == 200
    assert tested.json()["data"]["source"] == "TRANSIENT"
    assert "transient-browser-secret" not in tested.content.decode()
    config = MarketDataProviderConfiguration.objects.get(provider="FINNHUB")
    assert config.encrypted_api_key == ""
    assert config.api_key_last_four == ""
    assert config.last_test_success_at is not None
    assert responses.calls[-1].request.headers["X-Finnhub-Token"] == "transient-browser-secret"


def test_failed_history_fetch_run_is_committed_with_metadata():
    instrument = Instrument.objects.create(symbol="FAIL")

    class FailingClient:
        last_response_metadata = {"status_code": 429, "rate_limit": {"remaining": "0"}}

        def daily_candles(self, symbol, start_date, end_date):
            raise FinnhubError("rate limited", code="FINNHUB_RATE_LIMITED", status_code=429)

    with pytest.raises(FinnhubError, match="rate limited"):
        fetch_daily_history(
            instrument,
            date(2026, 1, 1),
            date(2026, 1, 31),
            purpose="OPTIMIZATION",
            client=FailingClient(),
        )

    run = MarketDataFetchRun.objects.get(instrument=instrument)
    assert run.status == "FAILED"
    assert run.error == "rate limited"
    assert run.completed_at is not None
    assert run.response_metadata["status_code"] == 429


def test_history_bulk_upsert_preserves_corrections_without_duplicate_rows():
    instrument=Instrument.objects.create(symbol="BULK")
    class Client:
        last_response_metadata={}
        def __init__(self,close):self.close=close
        def daily_candles(self,symbol,start_date,end_date):
            return [{"trading_date":date(2026,1,2),"open":"10","high":"12","low":"9",
                "close":self.close,"adjusted_close":self.close,"volume":"100",
                "provider_timestamp":"2026-01-02T00:00:00Z"}]
    first=fetch_daily_history(instrument,date(2026,1,1),date(2026,1,3),client=Client("10"))
    corrected=fetch_daily_history(instrument,date(2026,1,1),date(2026,1,3),client=Client("11"))
    row=InstrumentPriceHistory.objects.get(instrument=instrument,trading_date=date(2026,1,2))
    assert first.records_written==1 and corrected.records_written==0
    assert row.close==11 and InstrumentPriceHistory.objects.count()==1
