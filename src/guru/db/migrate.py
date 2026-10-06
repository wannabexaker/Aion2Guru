"""Forward-only SQL migrations.

Files: guru/db/migrations/NNNN_name.sql, applied in order, each in its own transaction.
Applied files are checksummed; editing an applied migration is an error (write a new one).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources

import asyncpg

_LOCK_KEY = 7_412_000  # pg_advisory_lock key reserved for migrations


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations() -> list[Migration]:
    root = resources.files("guru.db") / "migrations"
    items = [
        Migration(version=entry.name.removesuffix(".sql"), sql=entry.read_text(encoding="utf-8"))
        for entry in root.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(items, key=lambda m: m.version)


class MigrationError(RuntimeError):
    pass


async def apply_migrations(dsn: str) -> list[str]:
    """Apply pending migrations. Returns versions applied in this call."""
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    text PRIMARY KEY,
                   checksum   text NOT NULL,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        done = {
            r["version"]: r["checksum"] for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")
        }
        for mig in load_migrations():
            if mig.version in done:
                if done[mig.version] != mig.checksum:
                    raise MigrationError(f"migration {mig.version} was modified after being applied")
                continue
            async with conn.transaction():
                await conn.execute(mig.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                    mig.version,
                    mig.checksum,
                )
            applied_now.append(mig.version)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
        await conn.close()
    return applied_now


async def pending_migrations(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")
        done = set()
        if exists:
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        return [m.version for m in load_migrations() if m.version not in done]
    finally:
        await conn.close()
