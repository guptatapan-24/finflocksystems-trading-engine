#!/usr/bin/env python3
"""Create this Gateway's PostgreSQL database when it does not exist yet.

Runs from entrypoint.sh before migrations. A no-op when POSTGRES_HOST is unset
(the SQLite path used for local runs and tests).
"""
import os
import socket
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)


def ensure_database():
    host = os.getenv("POSTGRES_HOST", "").strip()
    if not host:
        print("POSTGRES_HOST is not set; using SQLite.", flush=True)
        return

    import psycopg
    from psycopg import sql
    from psycopg.errors import DuplicateDatabase

    port = int(os.getenv("POSTGRES_PORT", "5432"))
    name = os.getenv("POSTGRES_DB", "").strip()
    user = os.getenv("POSTGRES_USER", "").strip()
    password = os.getenv("POSTGRES_PASSWORD", "")
    missing = [key for key, value in (("POSTGRES_DB", name), ("POSTGRES_USER", user), ("POSTGRES_PASSWORD", password)) if not value]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} must be set when POSTGRES_HOST is set")

    max_attempts = int(os.getenv("DATABASE_BOOTSTRAP_MAX_ATTEMPTS", "30"))
    retry_seconds = float(os.getenv("DATABASE_BOOTSTRAP_RETRY_SECONDS", "2"))
    maintenance = {
        "host": host, "port": port, "user": user, "password": password,
        "dbname": os.getenv("POSTGRES_MAINTENANCE_DB", "postgres"), "connect_timeout": 5,
    }

    for attempt in range(1, max_attempts + 1):
        try:
            socket.getaddrinfo(host, port)
            with psycopg.connect(**maintenance, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                    if cursor.fetchone():
                        print(f"Database '{name}' already exists.", flush=True)
                        return
                    try:
                        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                    except DuplicateDatabase:
                        print(f"Database '{name}' was created concurrently.", flush=True)
                    else:
                        print(f"Created database '{name}'.", flush=True)
                    return
        except socket.gaierror:
            error = f"PostgreSQL hostname '{host}' cannot be resolved"
        except psycopg.OperationalError as exc:
            error = f"PostgreSQL at {host}:{port} is not reachable: {exc}"

        if attempt == max_attempts:
            raise RuntimeError(f"{error}. The Gateway container is probably not attached to the PostgreSQL Docker network.")
        print(f"{error}. Retrying in {retry_seconds:g} seconds ({attempt}/{max_attempts}).", file=sys.stderr, flush=True)
        time.sleep(retry_seconds)


if __name__ == "__main__":
    ensure_database()
