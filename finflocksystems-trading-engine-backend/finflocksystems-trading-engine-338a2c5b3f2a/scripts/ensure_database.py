#!/usr/bin/env python3
import os
import socket
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import DuplicateDatabase


BASE_DIR = Path(__file__).resolve().parent.parent

# QCH has no deployment environment configuration, so the Docker image
# copies .env.example to /app/.env. Load that file explicitly because this
# script runs before Django settings are initialized.
load_dotenv(BASE_DIR / ".env", override=False)


def ensure_database() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    connection_info = conninfo_to_dict(database_url)

    database_name = connection_info.get("dbname")
    database_host = connection_info.get("host")
    database_port = int(connection_info.get("port", 5432))

    if not database_name:
        raise RuntimeError(
            "DATABASE_URL does not contain a database name"
        )

    if not database_host:
        raise RuntimeError(
            "DATABASE_URL does not contain a database host"
        )

    max_attempts = int(
        os.getenv("DATABASE_BOOTSTRAP_MAX_ATTEMPTS", "30")
    )
    retry_seconds = float(
        os.getenv("DATABASE_BOOTSTRAP_RETRY_SECONDS", "2")
    )

    maintenance_connection_info = dict(connection_info)
    maintenance_connection_info["dbname"] = os.getenv(
        "POSTGRES_MAINTENANCE_DB",
        "postgres",
    )
    maintenance_connection_info["connect_timeout"] = 5

    for attempt in range(1, max_attempts + 1):
        try:
            socket.getaddrinfo(
                database_host,
                database_port,
            )

            with psycopg.connect(
                **maintenance_connection_info,
                autocommit=True,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1
                        FROM pg_database
                        WHERE datname = %s
                        """,
                        (database_name,),
                    )

                    if cursor.fetchone():
                        print(
                            f"Database '{database_name}' already exists.",
                            flush=True,
                        )
                        return

                    try:
                        cursor.execute(
                            sql.SQL("CREATE DATABASE {}").format(
                                sql.Identifier(database_name)
                            )
                        )
                    except DuplicateDatabase:
                        print(
                            f"Database '{database_name}' "
                            "was created concurrently.",
                            flush=True,
                        )
                    else:
                        print(
                            f"Created database '{database_name}'.",
                            flush=True,
                        )

                    return

        except socket.gaierror:
            error_message = (
                f"PostgreSQL hostname '{database_host}' "
                "cannot be resolved"
            )

        except psycopg.OperationalError as exc:
            error_message = (
                f"PostgreSQL at "
                f"{database_host}:{database_port} "
                f"is not reachable: {exc}"
            )

        if attempt == max_attempts:
            raise RuntimeError(
                f"{error_message}. "
                "The Backend container is probably not attached "
                "to the PostgreSQL Docker network."
            )

        print(
            f"{error_message}. "
            f"Retrying in {retry_seconds:g} seconds "
            f"({attempt}/{max_attempts}).",
            file=sys.stderr,
            flush=True,
        )

        time.sleep(retry_seconds)


if __name__ == "__main__":
    ensure_database()