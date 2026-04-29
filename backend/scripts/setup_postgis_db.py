#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege, InvalidCatalogName, InvalidPassword, UndefinedFile, UndefinedObject
from sqlalchemy.engine import URL, make_url


DEFAULT_DATABASE_URL: Final[str] = "postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
EXPECTED_TABLES: Final[tuple[str, ...]] = (
    "projects",
    "milestones",
    "milestone_metrics",
    "runs",
    "artifacts",
    "geometry_layers",
    "alembic_version",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create PostgreSQL role/database if needed, enable PostGIS, and optionally run Alembic migrations.",
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        help=(
            "SQLAlchemy database URL "
            "(e.g. postgresql+psycopg://building_change:building_change@localhost:5432/building_change)."
        ),
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Run Alembic migrations after provisioning the database.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify expected application tables exist.",
    )
    return parser.parse_args()


def resolve_database_url(cli_database_url: str | None) -> str:
    if cli_database_url:
        return cli_database_url
    env_database_url = os.getenv("DATABASE_URL")
    if env_database_url:
        return env_database_url
    return DEFAULT_DATABASE_URL


def redact_database_url(database_url: str) -> str:
    url = make_url(database_url)
    return url.render_as_string(hide_password=True)


def parse_target_url(database_url: str) -> URL:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must use a PostgreSQL driver.")
    if not url.database:
        raise RuntimeError("Target database name is missing from DATABASE_URL.")
    if not url.username:
        raise RuntimeError("Target role/username is missing from DATABASE_URL.")
    return url


def maintenance_url(target_url: URL) -> URL:
    return target_url.set(database="postgres")


def connect_from_url(url: URL) -> psycopg.Connection:
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
        autocommit=True,
    )


def connect_via_os_user(target_url: URL) -> psycopg.Connection:
    os_user = getpass.getuser()
    return psycopg.connect(
        dbname="postgres",
        user=os_user,
        host=None,
        port=target_url.port,
        autocommit=True,
    )


def connect_maintenance(target_url: URL) -> psycopg.Connection:
    primary_error: Exception | None = None
    try:
        return connect_from_url(maintenance_url(target_url))
    except (InvalidPassword, InvalidCatalogName, psycopg.OperationalError) as exc:
        primary_error = exc

    try:
        return connect_via_os_user(target_url)
    except Exception as fallback_error:  # noqa: BLE001
        message = (
            "Could not connect to PostgreSQL maintenance database. "
            "Tried target credentials and OS-user fallback."
        )
        if primary_error is not None:
            message = f"{message} First error: {primary_error.__class__.__name__}."
        raise RuntimeError(message) from fallback_error


def ensure_role_exists(connection: psycopg.Connection, role_name: str, password: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if cursor.fetchone():
            print(f"Role already exists: {role_name}")
            return
        if not password:
            raise RuntimeError(
                f"Role '{role_name}' does not exist and no password was provided in DATABASE_URL to create it."
            )
        try:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role_name),
                    sql.Literal(password),
                )
            )
        except InsufficientPrivilege as exc:
            raise RuntimeError(
                "Insufficient privileges to create role. "
                "Use a local PostgreSQL account that can create roles (or grant CREATEROLE)."
            ) from exc
        print(f"Created role: {role_name}")


def ensure_database_exists(connection: psycopg.Connection, database_name: str, owner_role: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
        if cursor.fetchone():
            print(f"Database already exists: {database_name}")
            return
        try:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(owner_role),
                )
            )
        except InsufficientPrivilege as exc:
            raise RuntimeError(
                "Insufficient privileges to create database. "
                "Use a local PostgreSQL account that can create databases (or grant CREATEDB)."
            ) from exc
        print(f"Created database: {database_name}")


def ensure_postgis(target_url: URL) -> str:
    try:
        with connect_from_url(target_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
                cursor.execute("SELECT PostGIS_Version();")
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    raise RuntimeError("PostGIS version check returned no value.")
                return str(row[0])
    except (UndefinedFile, UndefinedObject) as exc:
        raise RuntimeError(
            "PostGIS extension is not available in this PostgreSQL installation. "
            "Install PostGIS for your PostgreSQL version, then rerun this script."
        ) from exc


def run_migrations(database_url: str, backend_dir: Path) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PERSISTENCE_BACKEND"] = "postgres"

    try:
        subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=backend_dir,
            env=env,
            check=True,
        )
    except FileNotFoundError:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_dir,
            env=env,
            check=True,
        )
    print("Alembic migrations applied.")


def verify_tables(target_url: URL) -> list[str]:
    missing: list[str] = []
    with connect_from_url(target_url) as connection:
        with connection.cursor() as cursor:
            for table_name in EXPECTED_TABLES:
                cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    missing.append(table_name)
    return missing


def main() -> int:
    args = parse_args()
    backend_dir = Path(__file__).resolve().parents[1]

    try:
        database_url = resolve_database_url(args.database_url)
        target_url = parse_target_url(database_url)
        print(f"Using database URL: {redact_database_url(database_url)}")

        with connect_maintenance(target_url) as maintenance_connection:
            ensure_role_exists(maintenance_connection, target_url.username or "", target_url.password)
            ensure_database_exists(maintenance_connection, target_url.database or "", target_url.username or "")

        postgis_version = ensure_postgis(target_url)
        print(f"Database ready: {target_url.database}")
        print(f"PostGIS available: {postgis_version}")

        if args.migrate:
            run_migrations(database_url, backend_dir)

        if args.verify or args.migrate:
            missing = verify_tables(target_url)
            if missing:
                missing_rendered = ", ".join(missing)
                raise RuntimeError(f"Missing expected tables: {missing_rendered}")
            print("Verified expected tables exist.")

        print("PostgreSQL/PostGIS setup complete.")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Alembic migration failed with exit code {exc.returncode}.", file=sys.stderr)
        return 1
    except psycopg.OperationalError:
        print(
            "Could not connect to PostgreSQL. Ensure the server is running and credentials in DATABASE_URL are correct.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
