"""Actors: a single identity model for provenance and audit."""

from __future__ import annotations

import asyncpg


async def discord_actor(conn: asyncpg.Connection, discord_user_id: int, label: str = "") -> int:
    """Return the actor id for a Discord user, creating it on first sight.

    The label is not personal data we rely on; it defaults to the snowflake.
    """
    actor_id = await conn.fetchval(
        """INSERT INTO actors (kind, discord_user_id, label) VALUES ('discord_user', $1, $2)
           ON CONFLICT (discord_user_id) DO UPDATE SET discord_user_id = EXCLUDED.discord_user_id
           RETURNING id""",
        discord_user_id,
        label or str(discord_user_id),
    )
    return int(actor_id)


async def system_actor(conn: asyncpg.Connection, label: str, kind: str = "system") -> int:
    """Return the actor id for a system component (e.g. 'worker:extract', 'cli')."""
    actor_id = await conn.fetchval(
        """INSERT INTO actors (kind, label) VALUES ($1, $2)
           ON CONFLICT (kind, label) WHERE discord_user_id IS NULL
           DO UPDATE SET label = EXCLUDED.label
           RETURNING id""",
        kind,
        label,
    )
    return int(actor_id)
