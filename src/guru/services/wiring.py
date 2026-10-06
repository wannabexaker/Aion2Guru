"""Composition root: builds services and job handlers for the running roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from guru.jobs import queue
from guru.jobs.worker import Handler
from guru.llm.client import build_llm_client
from guru.llm.embeddings import build_embedder
from guru.services.extraction_service import ExtractionService
from guru.services.faq_service import FaqService
from guru.services.profiles import ProfileRegistry
from guru.services.web_service import WebService
from guru.web.fetcher import SafeFetcher
from guru.web.search import SearxNG

if TYPE_CHECKING:
    from guru.app import Runtime

SCHEDULES = (
    ("embed_backfill", "knowledge.embed", 600),
    ("learn_train", "learn.train", 86400),
    ("retention", "maintenance.retention", 3600),
    ("web_crawl", "web.crawl_due", 300),
    ("web_discover", "web.discover", 21600),
    ("web_reputation", "web.reputation", 86400),
    ("web_cleanup", "web.cleanup", 86400),
    ("faq_candidates", "faq.candidates", 3600),
    ("faq_check", "faq.check", 1800),
    ("faq_reconcile", "faq.reconcile", 900),
)


async def build_job_handlers(rt: Runtime) -> dict[str, Handler]:
    registry = ProfileRegistry(rt.db, frozenset(rt.settings.owner_ids))
    await registry.reload()
    await registry.listen(rt.dsn)
    llm = build_llm_client(rt.settings)
    embedder = build_embedder(rt.settings.embeddings)
    extraction = ExtractionService(rt.db, registry, llm, embedder)
    search = SearxNG(rt.settings.web.searxng_url) if rt.settings.web.searxng_url else None
    web = WebService(rt.db, registry, SafeFetcher(rt.settings.web), extraction, search)
    faq = FaqService(rt.db, registry, llm)
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
        "web.crawl_due": web.handle_crawl_due,
        "web.fetch_source": web.handle_fetch_source,
        "web.fetch_url": web.handle_fetch_url,
        "llm.web_extract": web.handle_web_extract,
        "web.discover": web.handle_discover,
        "web.reputation": web.handle_reputation,
        "web.cleanup": web.handle_cleanup,
        "faq.candidates": faq.handle_candidates,
        "faq.check": faq.handle_check,
        "faq.reconcile": faq.handle_reconcile,
    }
