"""Job worker: claims jobs, runs handlers with bounded concurrency, renews leases."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import asyncpg

from guru.db import Database
from guru.jobs import queue
from guru.jobs.queue import Job, PermanentJobError, RetryLater
from guru.logging import correlation, get_logger
from guru.observability import metrics

log = get_logger(__name__)

Handler = Callable[[Job], Awaitable[None]]


@dataclass
class WorkerConfig:
    concurrency: int = 2
    lease_seconds: int = 300
    poll_seconds: float = 5.0
    # Max concurrent jobs per kind prefix, e.g. {"llm.": 1} so background LLM work never saturates the GPU.
    kind_limits: dict[str, int] = field(default_factory=dict)


class Worker:
    def __init__(self, db: Database, dsn: str, handlers: dict[str, Handler], config: WorkerConfig) -> None:
        self.db = db
        self.dsn = dsn
        self.handlers = handlers
        self.config = config
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self._wake = asyncio.Event()
        self._slots = asyncio.Semaphore(config.concurrency)
        self._running: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ helpers
    def _prefix_of(self, kind: str) -> str | None:
        for prefix in self.config.kind_limits:
            if kind.startswith(prefix):
                return prefix
        return None

    def _claimable_kinds(self) -> list[str]:
        kinds = []
        for kind in self.handlers:
            prefix = self._prefix_of(kind)
            if prefix is None or self._running.get(prefix, 0) < self.config.kind_limits[prefix]:
                kinds.append(kind)
        return kinds

    def _on_notify(self, *_: object) -> None:
        self._wake.set()

    # ------------------------------------------------------------ main loop
    async def run(self, stop: asyncio.Event) -> None:
        listener: asyncpg.Connection | None = None
        try:
            listener = await asyncpg.connect(self.dsn)
            await listener.add_listener(queue.NOTIFY_CHANNEL, self._on_notify)
        except Exception:  # pragma: no cover - degraded to polling
            log.warning("worker.listen_failed", exc_info=True)
        log.info("worker.started", worker=self.name, kinds=sorted(self.handlers))
        last_housekeeping = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not stop.is_set():
                now = loop.time()
                if now - last_housekeeping > self.config.poll_seconds:
                    await self._housekeeping()
                    last_housekeeping = now
                claimed = await self._fill_slots()
                if not claimed:
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wait_any(stop), timeout=self.config.poll_seconds)
        finally:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if listener is not None:
                await listener.close()
            log.info("worker.stopped", worker=self.name)

    async def _wait_any(self, stop: asyncio.Event) -> None:
        waiters = [asyncio.ensure_future(self._wake.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def _housekeeping(self) -> None:
        try:
            async with self.db.connection() as conn:
                reclaimed = await queue.reclaim_expired(conn)
                if reclaimed:
                    log.warning("worker.leases_reclaimed", count=reclaimed)
                await queue.schedule_tick(conn)
        except Exception:
            log.error("worker.housekeeping_failed", exc_info=True)

    async def _fill_slots(self) -> int:
        claimed = 0
        while not self._slots.locked():
            kinds = self._claimable_kinds()
            if not kinds:
                break
            async with self.db.connection() as conn:
                job = await queue.claim(conn, self.name, kinds, self.config.lease_seconds)
            if job is None:
                break
            await self._slots.acquire()
            prefix = self._prefix_of(job.kind)
            if prefix:
                self._running[prefix] = self._running.get(prefix, 0) + 1
            task = asyncio.create_task(self._execute(job, prefix))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            claimed += 1
        return claimed

    async def run_once(self) -> bool:
        """Claim and execute a single job synchronously (tests, CLI). Returns False if none."""
        async with self.db.connection() as conn:
            job = await queue.claim(conn, self.name, list(self.handlers), self.config.lease_seconds)
        if job is None:
            return False
        await self._slots.acquire()
        await self._execute(job, None)
        return True

    async def _execute(self, job: Job, prefix: str | None) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        outcome = "done"
        try:
            with correlation(job.correlation_id):
                handler = self.handlers[job.kind]
                try:
                    await handler(job)
                    async with self.db.connection() as conn:
                        await queue.complete(conn, job.id)
                except RetryLater as exc:
                    outcome = "postponed"
                    async with self.db.connection() as conn:
                        await queue.postpone(conn, job, exc.delay_seconds, str(exc))
                except PermanentJobError as exc:
                    outcome = "dead"
                    log.warning("job.permanent_failure", job_id=job.id, kind=job.kind, error=str(exc))
                    async with self.db.connection() as conn:
                        await queue.fail(conn, job, str(exc), permanent=True)
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
                    async with self.db.connection() as conn:
                        outcome = await queue.fail(conn, job, detail)
                    log.error(
                        "job.failed",
                        job_id=job.id,
                        kind=job.kind,
                        attempts=job.attempts,
                        outcome=outcome,
                        exc_info=True,
                    )
        finally:
            heartbeat.cancel()
            metrics.JOBS_PROCESSED.labels(kind=job.kind, outcome=outcome).inc()
            if prefix:
                self._running[prefix] -= 1
            self._slots.release()
            self._wake.set()

    async def _heartbeat(self, job_id: int) -> None:
        interval = max(1.0, self.config.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.db.connection() as conn:
                    await queue.extend_lease(conn, job_id, self.name, self.config.lease_seconds)
            except Exception:
                log.warning("job.heartbeat_failed", job_id=job_id, exc_info=True)
