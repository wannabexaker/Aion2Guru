"""Process runner: one image, roles selected at start (bot, worker, api)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import uvicorn

from guru.api.app import create_app
from guru.db import Database, create_database
from guru.jobs.worker import Worker, WorkerConfig
from guru.logging import configure_logging, get_logger
from guru.settings import Settings

log = get_logger(__name__)

ROLES = ("bot", "worker", "api")


class Runtime:
    """Shared state for all roles running in this process."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.stop = asyncio.Event()
        self.ready: dict[str, bool] = {}

    @property
    def dsn(self) -> str:
        return self.settings.database_url.get_secret_value()


class _QuietServer(uvicorn.Server):
    """uvicorn without its own signal handling: the runtime owns SIGINT/SIGTERM."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _run_api(rt: Runtime) -> None:
    app = create_app(rt.db, readiness=lambda: dict(rt.ready))
    config = uvicorn.Config(
        app, host=rt.settings.api.host, port=rt.settings.api.port, log_level="warning", lifespan="off"
    )
    server = _QuietServer(config)
    task = asyncio.create_task(server.serve())
    await rt.stop.wait()
    server.should_exit = True
    await task


async def _run_worker(rt: Runtime) -> None:
    from guru.services.wiring import build_job_handlers

    handlers = await build_job_handlers(rt)
    cfg = rt.settings.worker
    worker = Worker(
        rt.db,
        rt.dsn,
        handlers,
        WorkerConfig(
            concurrency=cfg.concurrency,
            lease_seconds=cfg.lease_seconds,
            poll_seconds=cfg.poll_seconds,
            kind_limits={"llm.": 1},
        ),
    )
    rt.ready["worker"] = True
    await worker.run(rt.stop)


async def _run_bot(rt: Runtime) -> None:
    from guru.discord_bot.client import run_bot

    await run_bot(rt)


ROLE_RUNNERS: dict[str, Callable[[Runtime], Coroutine[Any, Any, None]]] = {
    "api": _run_api,
    "worker": _run_worker,
    "bot": _run_bot,
}


async def run(settings: Settings, roles: list[str]) -> None:
    configure_logging(settings.log_level, settings.log_json)
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise SystemExit(f"unknown roles: {sorted(unknown)}")
    db = await create_database(settings.database_url.get_secret_value())
    rt = Runtime(settings, db)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rt.stop.set)
    log.info("guru.start", roles=roles)
    tasks = [asyncio.create_task(ROLE_RUNNERS[r](rt), name=f"role:{r}") for r in roles]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception() is not None:
                log.error("guru.role_crashed", role=t.get_name(), exc_info=t.exception())
                rt.stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await db.close()
        log.info("guru.stopped")
