import os


os.environ["DJANGO_SECRET_KEY"] = "gateway-unit-test-django-secret"
os.environ["GATEWAY_SERVICE_TOKEN"] = "test-token"
os.environ["BROKER_ADAPTER"] = "mock"
os.environ["IBC_TRADING_MODE"] = "paper"

from .settings import *  # noqa: F403,E402
