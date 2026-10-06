"""Hour/day usage counters for rate limits (atomic increment-and-check)."""

from __future__ import annotations

from datetime import datetime

import asyncpg


def window_start(now: datetime, kind: str) -> datetime:
    if kind == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def take(
    conn: asyncpg.Connection,
    profile_id: int,
    user_hash: bytes,
    bucket: str,
    kind: str,
    limit: int | None,
    now: datetime,
) -> bool:
    """Consume one unit if under the limit. Never over-counts on rejection."""
    if limit is None:
        return True
    if limit <= 0:
        return False
    start = window_start(now, kind)
    count = await conn.fetchval(
        """INSERT INTO usage_counters (profile_id, user_hash, bucket, window_kind, window_start, count)
           VALUES ($1, $2, $3, $4, $5, 1)
           ON CONFLICT (profile_id, user_hash, bucket, window_kind, window_start)
           DO UPDATE SET count = usage_counters.count + 1
           WHERE usage_counters.count < $6
           RETURNING count""",
        profile_id,
        user_hash,
        bucket,
        kind,
        start,
        limit,
    )
    return count is not None


async def gc(conn: asyncpg.Connection, older_than: datetime) -> int:
    status = await conn.execute("DELETE FROM usage_counters WHERE window_start < $1", older_than)
    return int(str(status).split()[-1])
