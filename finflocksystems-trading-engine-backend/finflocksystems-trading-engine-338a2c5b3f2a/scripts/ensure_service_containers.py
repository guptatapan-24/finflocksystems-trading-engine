#!/usr/bin/env python3
"""Create the Kafka / Flink sub-containers (apps/broker_gateway/platform_services.py) before the Backend waits for them.

Run from entrypoint.sh ahead of ensure_kafka_topics.py. A no-op unless PLATFORM_SERVICES_ENABLED=true. Never blocks the start: any problem
is printed and the entrypoint carries on (the existing Kafka/Flink checks then report what is missing, as they do today).
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main() -> int:
    import django

    django.setup()
    from apps.broker_gateway import platform_services

    result = platform_services.ensure_all()
    print("platform services:", ", ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - must never stop the Backend from starting
        print(f"platform services: skipped ({type(exc).__name__})", file=sys.stderr)
        sys.exit(0)
