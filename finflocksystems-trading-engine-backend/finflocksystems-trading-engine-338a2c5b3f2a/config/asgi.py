import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

django_application = get_asgi_application()

from apps.broker_gateway.proxy import BrokerProxyRouter


application = BrokerProxyRouter(django_application)
