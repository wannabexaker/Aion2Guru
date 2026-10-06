"""Postgres-backed job queue (SKIP LOCKED + leases + LISTEN/NOTIFY). No Redis.

Producers call `enqueue` inside their own transaction (transactional outbox): the job
exists if and only if the state change that caused it committed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from guru.logging import current_correlation_id

NOTIFY_CHANNEL = "guru_jobs"


class PermanentJobError(Exception):
    """The job can never succeed (bad payload, missing target). Goes straight to dead-letter."""


class RetryLater(Exception):
    """Re-queue without counting as a failure-worthy error (e.g. dependency not ready)."""

    def __init__(self, delay_seconds: float, reason: str = "") -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    correlation_id: str | None


async def enqueue(
    conn: asyncpg.Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job. Returns its id, or None if the idempotency key already exists."""
    job_id = await conn.fetchval(
        """INSERT INTO jobs (kind, payload, priority, idempotency_key, run_after, max_attempts, correlation_id)
           VALUES ($1, $2, $3, $4, coalesce($5, now()), $6, $7)
           ON CONFLICT (idempotency_key) DO NOTHING
           RETURNING id""",
        kind,
        payload or {},
        priority,
        idempotency_key,
        run_after,
        max_attempts,
        current_correlation_id(),
    )
    if job_id is not None:
        await conn.execute("SELECT pg_notify($1, $2)", NOTIFY_CHANNEL, kind)
    return None if job_id is None else int(job_id)


async def claim(conn: asyncpg.Connection, worker: str, kinds: list[str], lease_seconds: int) -> Job | None:
    row = await conn.fetchrow(
        """UPDATE jobs
              SET status = 'running', locked_by = $1,
                  locked_until = now() + make_interval(secs => $2), attempts = attempts + 1
            WHERE id = (SELECT id FROM jobs
                         WHERE status = 'queued' AND run_after <= now() AND kind = ANY($3::text[])
                         ORDER BY priority, run_after, id
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1)
        RETURNING id, kind, payload, attempts, max_attempts, correlation_id""",
        worker,
        lease_seconds,
        kinds,
    )
    if row is None:
        return None
    return Job(
        id=row["id"],
        kind=row["kind"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        correlation_id=row["correlation_id"],
    )


async def complete(conn: asyncpg.Connection, job_id: int) -> None:
    await conn.execute(
        """UPDATE jobs SET status = 'done', finished_at = now(), locked_by = NULL, locked_until = NULL,
                  last_error = NULL
            WHERE id = $1""",
        job_id,
    )


def backoff_seconds(attempts: int, base: float = 5.0, cap: float = 3600.0) -> float:
    """Exponential backoff with jitter in [50%, 100%]."""
    raw = min(cap, base * float(2 ** max(0, attempts - 1)))
    return float(raw * random.uniform(0.5, 1.0))


async def fail(conn: asyncpg.Connection, job: Job, error: str, *, permanent: bool = False) -> str:
    """Record a failure. Returns the new status ('queued' or 'dead')."""
    dead = permanent or job.attempts >= job.max_attempts
    status = "dead" if dead else "queued"
    await conn.execute(
        """UPDATE jobs
              SET status = $2, last_error = left($3, 4000), locked_by = NULL, locked_until = NULL,
                  run_after = CASE WHEN $2 = 'queued' THEN now() + make_interval(secs => $4) ELSE run_after END,
                  finished_at = CASE WHEN $2 = 'dead' THEN now() ELSE NULL END
            WHERE id = $1""",
        job.id,
        status,
        error,
        backoff_seconds(job.attempts),
    )
    return status


async def postpone(conn: asyncpg.Connection, job: Job, delay_seconds: float, reason: str) -> None:
    """Re-queue without consuming an attempt."""
    await conn.execute(
        """UPDATE jobs
              SET status = 'queued', attempts = greatest(attempts - 1, 0), last_error = $3,
                  locked_by = NULL, locked_until = NULL, run_after = now() + make_interval(secs => $2)
            WHERE id = $1""",
        job.id,
        delay_seconds,
        reason,
    )


async def extend_lease(conn: asyncpg.Connection, job_id: int, worker: str, lease_seconds: int) -> bool:
    status = await conn.execute(
        """UPDATE jobs SET locked_until = now() + make_interval(secs => $3)
            WHERE id = $1 AND locked_by = $2 AND status = 'running'""",
        job_id,
        worker,
        lease_seconds,
    )
    return str(status).endswith(" 1")


async def reclaim_expired(conn: asyncpg.Connection) -> int:
    """Leases that expired belong to crashed workers: make the jobs claimable again."""
    status = await conn.execute(
        """UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL,
                  last_error = coalesce(last_error, 'lease expired')
            WHERE status = 'running' AND locked_until < now()"""
    )
    return int(str(status).split()[-1])


async def retry_dead(conn: asyncpg.Connection, job_id: int) -> bool:
    status = await conn.execute(
        """UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), finished_at = NULL
            WHERE id = $1 AND status = 'dead'""",
        job_id,
    )
    return str(status).endswith(" 1")


async def stats(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """SELECT kind, status, count(*) AS n, min(run_after) AS oldest
                 FROM jobs WHERE status <> 'done' GROUP BY kind, status ORDER BY kind, status"""
        )
    )


# ------------------------------------------------------------------ schedules


async def ensure_schedule(
    conn: asyncpg.Connection,
    key: str,
    kind: str,
    interval_seconds: int,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO schedules (key, kind, payload, interval_seconds)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind, payload = EXCLUDED.payload,
                                         interval_seconds = EXCLUDED.interval_seconds""",
        key,
        kind,
        payload or {},
        interval_seconds,
    )


async def schedule_tick(conn: asyncpg.Connection) -> int:
    """Enqueue jobs for due schedules. Safe to run from several workers concurrently."""
    async with conn.transaction():
        due = await conn.fetch(
            """WITH due AS (
                   SELECT key FROM schedules WHERE enabled AND next_run_at <= now()
                   FOR UPDATE SKIP LOCKED)
               UPDATE schedules s
                  SET next_run_at = now() + make_interval(secs => s.interval_seconds)
                 FROM due WHERE s.key = due.key
               RETURNING s.key, s.kind, s.payload, s.interval_seconds,
                         floor(extract(epoch FROM now()) / s.interval_seconds)::bigint AS bucket"""
        )
        for row in due:
            await enqueue(
                conn,
                row["kind"],
                row["payload"],
                priority=200,
                idempotency_key=f"schedule:{row['key']}:{row['bucket']}",
            )
    return len(due)
