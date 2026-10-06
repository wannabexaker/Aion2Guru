"""asyncpg connection pool with jsonb and pgvector codecs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from guru.db.migrate import apply_migrations


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    has_vector = await conn.fetchval("SELECT 1 FROM pg_type WHERE typname = 'vector'")
    if has_vector:
        from pgvector.asyncpg import register_vector

        await register_vector(conn)


class Database:
    """Thin wrapper: a pool plus helpers for transactions."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return str(await conn.execute(sql, *args))

    async def close(self) -> None:
        await self.pool.close()


async def create_database(dsn: str, *, migrate: bool = True, min_size: int = 1, max_size: int = 10) -> Database:
    if migrate:
        await apply_migrations(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_connection)
    assert pool is not None
    return Database(pool)
