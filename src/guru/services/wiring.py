"""Composition root: builds services and job handlers for the running roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from guru.jobs import queue
from guru.jobs.worker import Handler
from guru.llm.client import build_llm_client
from guru.llm.embeddings import build_embedder
from guru.services.extraction_service import ExtractionService
from guru.services.profiles import ProfileRegistry

if TYPE_CHECKING:
    from guru.app import Runtime

SCHEDULES = (
    ("embed_backfill", "knowledge.embed", 600),
    ("learn_train", "learn.train", 86400),
    ("retention", "maintenance.retention", 3600),
)


async def build_job_handlers(rt: Runtime) -> dict[str, Handler]:
    registry = ProfileRegistry(rt.db, frozenset(rt.settings.owner_ids))
    await registry.reload()
    await registry.listen(rt.dsn)
    llm = build_llm_client(rt.settings)
    embedder = build_embedder(rt.settings.embeddings)
    extraction = ExtractionService(rt.db, registry, llm, embedder)
    async with rt.db.connection() as conn:
        for key, kind, interval in SCHEDULES:
            await queue.ensure_schedule(conn, key, kind, interval)
    return {
        "llm.window": extraction.handle_window,
        "llm.extract": extraction.handle_extract,
        "knowledge.recompute": extraction.handle_recompute,
        "knowledge.embed": extraction.handle_embed_backfill,
        "learn.train": extraction.handle_train,
        "maintenance.retention": extraction.handle_retention,
    }
