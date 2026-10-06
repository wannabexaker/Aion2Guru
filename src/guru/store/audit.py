"""Append-only audit log. Hash chaining is done by a DB trigger (see 0001_core.sql)."""

from __future__ import annotations

from typing import Any

import asyncpg

from guru.logging import current_correlation_id


async def record(
    conn: asyncpg.Connection,
    *,
    actor_id: int,
    action: str,
    profile_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> int:
    """Write one audit row inside the caller's transaction."""
    row_id = await conn.fetchval(
        """INSERT INTO audit_log
             (actor_id, action, profile_id, target_type, target_id, before, after, reason, correlation_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
           RETURNING id""",
        actor_id,
        action,
        profile_id,
        target_type,
        None if target_id is None else str(target_id),
        before,
        after,
        reason,
        current_correlation_id(),
    )
    return int(row_id)


async def verify_chain(conn: asyncpg.Connection) -> int | None:
    """Return the first broken chain_seq, or None if the chain is intact."""
    broken = await conn.fetchval("SELECT audit_verify_chain()")
    return None if broken is None else int(broken)


async def query(
    conn: asyncpg.Connection,
    *,
    profile_id: int | None = None,
    action_prefix: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 20,
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """SELECT a.chain_seq, a.ts, a.action, a.target_type, a.target_id, a.reason,
                      ac.kind AS actor_kind, ac.discord_user_id, ac.label AS actor_label
               FROM audit_log a JOIN actors ac ON ac.id = a.actor_id
               WHERE ($1::bigint IS NULL OR a.profile_id = $1)
                 AND ($2::text IS NULL OR a.action LIKE $2 || '%')
                 AND ($3::text IS NULL OR a.target_type = $3)
                 AND ($4::text IS NULL OR a.target_id = $4)
               ORDER BY a.chain_seq DESC
               LIMIT $5""",
            profile_id,
            action_prefix,
            target_type,
            target_id,
            limit,
        )
    )
