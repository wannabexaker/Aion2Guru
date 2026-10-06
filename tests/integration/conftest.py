"""Integration fixtures: a throwaway database on a real PostgreSQL + pgvector server.

Set GURU_TEST_DATABASE_URL to an admin DSN (e.g. postgresql://postgres@localhost:5432/postgres).
Each test session creates and drops its own database. No mocks for SQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from guru.db import Database, create_database

ADMIN_URL = os.environ.get("GURU_TEST_DATABASE_URL")

pytestmark = pytest.mark.db


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@pytest.fixture(scope="session")
async def db_url() -> AsyncIterator[str]:
    if not ADMIN_URL:
        pytest.skip("GURU_TEST_DATABASE_URL not set")
    name = f"guru_test_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    try:
        yield _with_db(ADMIN_URL, name)
    finally:
        admin = await asyncpg.connect(ADMIN_URL)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture(scope="session")
async def database(db_url: str) -> AsyncIterator[Database]:
    db = await create_database(db_url, max_size=12)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def db(database: Database) -> AsyncIterator[Database]:
    """Clean state per test (TRUNCATE does not fire row triggers, so the audit guard is bypassed)."""
    tables = await database.fetch(
        """SELECT quote_ident(tablename) AS t FROM pg_tables
            WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"""
    )
    await database.execute(f"TRUNCATE {', '.join(r['t'] for r in tables)} RESTART IDENTITY CASCADE")
    yield database
