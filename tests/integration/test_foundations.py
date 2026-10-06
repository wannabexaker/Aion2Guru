from __future__ import annotations

import asyncio

import asyncpg
import pytest

from guru.db import Database
from guru.db.migrate import apply_migrations, load_migrations, pending_migrations
from guru.jobs import queue
from guru.jobs.queue import Job, PermanentJobError, RetryLater
from guru.jobs.worker import Worker, WorkerConfig
from guru.store import actors, audit

pytestmark = pytest.mark.db


# ---------------------------------------------------------------- migrations


async def test_migrations_idempotent(db_url: str, database: Database) -> None:
    assert await apply_migrations(db_url) == []
    assert await pending_migrations(db_url) == []
    versions = [r["version"] for r in await database.fetch("SELECT version FROM schema_migrations ORDER BY 1")]
    assert versions == [m.version for m in load_migrations()]


async def test_extensions_and_fts(db: Database) -> None:
    exts = {r["extname"] for r in await db.fetch("SELECT extname FROM pg_extension")}
    assert {"vector", "pg_trgm"} <= exts
    tsv = await db.fetchval("SELECT to_tsvector('guru_simple', 'boss respawn 4h')::text")
    assert "respawn" in tsv


# ---------------------------------------------------------------- audit


async def test_audit_chain_and_append_only(db: Database) -> None:
    async with db.transaction() as conn:
        actor = await actors.system_actor(conn, "test")
        for i in range(5):
            await audit.record(conn, actor_id=actor, action="test.write", target_type="x", target_id=i, after={"i": i})
    async with db.connection() as conn:
        assert await audit.verify_chain(conn) is None
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("UPDATE audit_log SET reason = 'x' WHERE chain_seq = 2")
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("DELETE FROM audit_log WHERE chain_seq = 2")
        # Simulate tampering by a privileged user bypassing the guard trigger.
        await conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_immutable")
        await conn.execute("""UPDATE audit_log SET after = '{"i": 99}' WHERE chain_seq = 3""")
        await conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_immutable")
        assert await audit.verify_chain(conn) == 3


async def test_audit_chain_concurrent_writers(db: Database) -> None:
    async with db.transaction() as conn:
        actor = await actors.system_actor(conn, "test")

    async def writer(n: int) -> None:
        for i in range(10):
            async with db.transaction() as conn:
                await audit.record(conn, actor_id=actor, action="test.concurrent", target_id=f"{n}-{i}")

    await asyncio.gather(*(writer(n) for n in range(5)))
    async with db.connection() as conn:
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 50
        assert await audit.verify_chain(conn) is None


async def test_actor_upserts_are_stable(db: Database) -> None:
    async with db.transaction() as conn:
        a = await actors.discord_actor(conn, 1234)
        b = await actors.discord_actor(conn, 1234)
        s1 = await actors.system_actor(conn, "worker:x")
        s2 = await actors.system_actor(conn, "worker:x")
    assert a == b and s1 == s2 and a != s1


# ---------------------------------------------------------------- job queue


async def test_enqueue_idempotency(db: Database) -> None:
    async with db.transaction() as conn:
        first = await queue.enqueue(conn, "t.kind", {"a": 1}, idempotency_key="k1")
        second = await queue.enqueue(conn, "t.kind", {"a": 2}, idempotency_key="k1")
    assert first is not None and second is None


async def test_enqueue_rolls_back_with_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await queue.enqueue(conn, "t.kind", {})
            raise RuntimeError("abort")
    assert await db.fetchval("SELECT count(*) FROM jobs") == 0


async def test_no_double_processing_under_concurrency(db: Database, db_url: str) -> None:
    async with db.transaction() as conn:
        for i in range(60):
            await queue.enqueue(conn, "t.count", {"i": i})
    seen: list[int] = []

    async def handler(job: Job) -> None:
        await asyncio.sleep(0)
        seen.append(job.payload["i"])

    workers = [Worker(db, db_url, {"t.count": handler}, WorkerConfig(concurrency=3)) for _ in range(4)]
    for idx, w in enumerate(workers):
        w.name = f"w{idx}"

    async def drain(w: Worker) -> None:
        while await w.run_once():
            pass

    await asyncio.gather(*(drain(w) for w in workers))
    assert sorted(seen) == list(range(60))
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE status = 'done'") == 60


async def test_failure_backoff_and_dead_letter(db: Database, db_url: str) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, "t.fail", {}, max_attempts=2)
        await queue.enqueue(conn, "t.perm", {})
        await queue.enqueue(conn, "t.later", {})

    async def boom(job: Job) -> None:
        raise ValueError("transient")

    async def perm(job: Job) -> None:
        raise PermanentJobError("bad payload")

    async def later(job: Job) -> None:
        raise RetryLater(60, "not ready")

    w = Worker(db, db_url, {"t.fail": boom, "t.perm": perm, "t.later": later}, WorkerConfig())
    while await w.run_once():
        pass
    rows = {r["kind"]: r for r in await db.fetch("SELECT kind, status, attempts, last_error FROM jobs")}
    assert rows["t.perm"]["status"] == "dead"
    assert rows["t.later"]["status"] == "queued" and rows["t.later"]["attempts"] == 0
    assert rows["t.fail"]["status"] == "queued" and rows["t.fail"]["attempts"] == 1
    # Second attempt exhausts max_attempts → dead.
    await db.execute("UPDATE jobs SET run_after = now() WHERE kind = 't.fail'")
    await w.run_once()
    assert await db.fetchval("SELECT status FROM jobs WHERE kind = 't.fail'") == "dead"
    async with db.connection() as conn:
        job_id = await conn.fetchval("SELECT id FROM jobs WHERE kind = 't.fail'")
        assert await queue.retry_dead(conn, job_id)


async def test_expired_lease_is_reclaimed(db: Database) -> None:
    async with db.transaction() as conn:
        await queue.enqueue(conn, "t.lease", {})
    async with db.connection() as conn:
        job = await queue.claim(conn, "crashed", ["t.lease"], lease_seconds=1)
        assert job is not None
        await conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second'")
        assert await queue.reclaim_expired(conn) == 1
        again = await queue.claim(conn, "healthy", ["t.lease"], lease_seconds=60)
        assert again is not None and again.id == job.id and again.attempts == 2


async def test_schedule_tick_enqueues_once_per_bucket(db: Database) -> None:
    async with db.connection() as conn:
        await queue.ensure_schedule(conn, "nightly", "t.sched", 3600, {"x": 1})
        assert await queue.schedule_tick(conn) == 1
        assert await queue.schedule_tick(conn) == 0  # next_run_at moved forward
        await conn.execute("UPDATE schedules SET next_run_at = now()")
        await queue.schedule_tick(conn)  # same bucket → idempotency key collides
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 't.sched'") == 1


async def test_worker_run_loop_wakes_on_notify(db: Database, db_url: str) -> None:
    done = asyncio.Event()

    async def handler(job: Job) -> None:
        done.set()

    w = Worker(db, db_url, {"t.notify": handler}, WorkerConfig(poll_seconds=30))
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.3)
    async with db.transaction() as conn:
        await queue.enqueue(conn, "t.notify", {})
    await asyncio.wait_for(done.wait(), timeout=5)  # far below the 30 s poll interval
    stop.set()
    await asyncio.wait_for(task, timeout=5)
