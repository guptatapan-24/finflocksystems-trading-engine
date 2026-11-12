import pytest
from django.test import override_settings


@pytest.mark.django_db
@override_settings(CORS_ALLOWED_ORIGINS=["http://localhost:5173"])
def test_cors_preflight_allows_idempotency_header(client):
    response=client.options("/api/v1/orders/",HTTP_ORIGIN="http://localhost:5173",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,idempotency-key")
    assert response.status_code==200
    assert response["Access-Control-Allow-Origin"]=="http://localhost:5173"
    assert "idempotency-key" in response["Access-Control-Allow-Headers"].lower()
