from django.conf import settings


def create_adapter():
    if settings.BROKER_ADAPTER == "mock":
        from .mock import MockBrokerAdapter
        return MockBrokerAdapter()
    if settings.BROKER_ADAPTER == "ib_async":
        from .ib_async_adapter import IBAsyncBrokerAdapter
        return IBAsyncBrokerAdapter()
    raise ValueError("BROKER_ADAPTER must be exactly ib_async or mock")
