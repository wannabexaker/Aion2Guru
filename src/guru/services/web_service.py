"""Web ingestion & validation (DESIGN §8, D-11): crawl, extract, date, de-duplicate, chunk, extract claims,
discover new sources, and adjust source trust from agreement with verified knowledge."""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import re2
from tld import get_fld

from guru.core.ingest import SourceMessage
from guru.core.permissions import Principal
from guru.core.text import normalize
from guru.db import Database
from guru.jobs import queue
from guru.jobs.queue import Job, PermanentJobError, RetryLater
from guru.llm.client import LLMBadOutput, LLMUnavailable
from guru.logging import get_logger
from guru.services.extraction_service import ExtractionService
from guru.services.knowledge_service import PermissionDenied
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.store import actors, audit, usage
from guru.store import knowledge as kstore
from guru.web.extract import (
    NEAR_DUP_BITS,
    chunk_text,
    extract_document,
    feed_entries,
    hamming,
    simhash,
    sitemap_urls,
    to_signed64,
)
from guru.web.fetcher import FetchBlocked, SafeFetcher, validate_url
from guru.web.search import SearchProvider

log = get_logger(__name__)


def registrable_domain(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return get_fld(url, fail_silently=True) or host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def url_key(url: str) -> str:
    """Canonical-ish URL for identity: no fragment, no trailing slash, lower-case host."""
    p = urlsplit(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{(p.hostname or '').lower()}{path}" + (f"?{p.query}" if p.query else "")


class WebService:
    def __init__(
        self,
        db: Database,
        registry: ProfileRegistry,
        fetcher: SafeFetcher,
        extraction: ExtractionService | None,
        search: SearchProvider | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.fetcher = fetcher
        self.extraction = extraction
        self.search = search

    async def _state(self, profile_id: int) -> ProfileState:
        state = self.registry.get(profile_id)
        if state is None:
            await self.registry.reload()
            state = self.registry.get(profile_id)
        if state is None:
            raise PermanentJobError(f"profile {profile_id} not active")
        return state

    # ------------------------------------------------------------ sources
    @staticmethod
    def domain_rule(state: ProfileState, domain: str) -> tuple[int, str] | None:
        """(tier, independence group) for a dynamic domain, or None if denied."""
        cfg = state.config
        if any(fnmatch.fnmatch(domain, d) for d in cfg.domain_deny):
            return None
        for rule in cfg.domain_trust:
            if fnmatch.fnmatch(domain, rule.pattern):
                return rule.tier, rule.group or f"site:{domain}"
        return 1, f"site:{domain}"

    async def dynamic_source(self, conn: asyncpg.Connection, state: ProfileState, url: str) -> int | None:
        domain = registrable_domain(url)
        rule = self.domain_rule(state, domain)
        if rule is None:
            return None
        tier, group = rule
        source_id = await conn.fetchval(
            """INSERT INTO sources (profile_id, key, kind, name, locator, domain, independence_group, trust_tier,
                                    origin, schedule_seconds)
               VALUES ($1, $2, 'web_page', $3, $4, $3, $5, $6, 'dynamic', NULL)
               ON CONFLICT (profile_id, key) DO UPDATE SET enabled = sources.enabled
               RETURNING id""",
            state.profile_id,
            f"site:{domain}",
            domain,
            f"https://{domain}/",
            group,
            tier,
        )
        return int(source_id)

    async def ingest_url(self, state: ProfileState, principal: Principal, url: str) -> int:
        """/kb ingest-url — explicit web capture by a contributor. Returns the job id."""
        if not state.resolver.has(principal, "kb.ingest"):
            raise PermissionDenied("kb.ingest")
        validate_url(url)
        async with self.db.transaction() as conn:
            source_id = await self.dynamic_source(conn, state, url)
            if source_id is None:
                raise ValueError("this domain is blocked for this profile")
            actor_id = await actors.discord_actor(conn, principal.user_id)
            await audit.record(
                conn,
                actor_id=actor_id,
                action="source.ingest_url",
                profile_id=state.profile_id,
                target_type="url",
                target_id=url[:500],
            )
            job_id = await queue.enqueue(
                conn,
                "web.fetch_url",
                {"profile_id": state.profile_id, "source_id": source_id, "url": url, "mode": "explicit"},
                priority=30,
                idempotency_key=f"ingest_url:{url_key(url)}:{datetime.now(UTC):%Y%m%d%H}",
            )
        return job_id or 0

    # ------------------------------------------------------------ crawl scheduling
    async def handle_crawl_due(self, job: Job) -> None:
        async with self.db.transaction() as conn:
            rows = await conn.fetch(
                """SELECT id, profile_id FROM sources
                    WHERE enabled AND kind IN ('web_page', 'web_feed', 'web_sitemap')
                      AND origin IN ('config', 'dynamic') AND locator IS NOT NULL
                      AND coalesce(next_fetch_at, now()) <= now()
                      AND (circuit_open_until IS NULL OR circuit_open_until < now())
                      AND (origin = 'config' OR schedule_seconds IS NOT NULL)
                    ORDER BY next_fetch_at NULLS FIRST LIMIT 25"""
            )
            hour = datetime.now(UTC).strftime("%Y%m%d%H")
            for r in rows:
                await queue.enqueue(
                    conn,
                    "web.fetch_source",
                    {"profile_id": r["profile_id"], "source_id": r["id"]},
                    priority=150,
                    idempotency_key=f"fetch_source:{r['id']}:{hour}",
                )

    async def handle_fetch_source(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        src = await self.db.fetchrow("SELECT * FROM sources WHERE id = $1", job.payload["source_id"])
        if src is None or not src["enabled"]:
            return
        next_in = src["schedule_seconds"] or state.config.web.recrawl_seconds
        try:
            if src["kind"] == "web_page":
                await self.fetch_document(state, dict(src), src["locator"], mode="crawl")
            else:
                res = await self.fetcher.fetch(src["locator"], etag=src["etag"], last_modified=src["last_modified"])
                if res.status >= 400:
                    raise ValueError(f"http {res.status}")
                if not res.not_modified:
                    limit = state.config.web.max_urls_per_source
                    urls = feed_entries(res.body, limit) if src["kind"] == "web_feed" else sitemap_urls(res.body, limit)
                    include = (src["fetch_config"] or {}).get("include")
                    pattern = re2.compile(include) if include else None
                    async with self.db.transaction() as conn:
                        for url, _ in urls:
                            if pattern is not None and not pattern.search(url):
                                continue
                            await queue.enqueue(
                                conn, "web.fetch_url",
                                {"profile_id": state.profile_id, "source_id": src["id"], "url": url, "mode": "crawl"},
                                priority=160, idempotency_key=f"fetch_url:{src['id']}:{url_key(url)}:{res.etag or ''}",
                            )  # fmt: skip
                    await self.db.execute(
                        "UPDATE sources SET etag = $2, last_modified = $3 WHERE id = $1",
                        src["id"],
                        res.etag,
                        res.last_modified,
                    )
            await self.db.execute(
                """UPDATE sources SET last_fetched_at = now(), consecutive_failures = 0, circuit_open_until = NULL,
                          next_fetch_at = now() + make_interval(secs => $2) WHERE id = $1""",
                src["id"],
                next_in,
            )
        except (FetchBlocked, httpx.HTTPError, ValueError) as exc:
            failures = int(src["consecutive_failures"]) + 1
            await self.db.execute(
                """UPDATE sources SET consecutive_failures = $2,
                          circuit_open_until = CASE WHEN $2 >= 3 THEN now() + make_interval(hours => least($2, 48))
                                                    ELSE circuit_open_until END,
                          next_fetch_at = now() + make_interval(secs => $3) WHERE id = $1""",
                src["id"],
                failures,
                next_in,
            )
            log.warning("web.source_failed", source=src["key"], error=str(exc)[:200], failures=failures)

    async def handle_fetch_url(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        src = await self.db.fetchrow("SELECT * FROM sources WHERE id = $1", job.payload["source_id"])
        if src is None:
            return
        try:
            await self.fetch_document(state, dict(src), job.payload["url"], mode=job.payload.get("mode", "crawl"))
        except FetchBlocked as exc:
            raise PermanentJobError(str(exc)) from exc

    # ------------------------------------------------------------ documents
    async def fetch_document(self, state: ProfileState, src: dict[str, Any], url: str, *, mode: str) -> str:
        key = url_key(url)
        async with self.db.connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id, meta, content_hash FROM observations WHERE source_id = $1 AND external_id = $2",
                src["id"],
                key,
            )
        meta = dict(existing["meta"]) if existing else {}
        res = await self.fetcher.fetch(url, etag=meta.get("etag"), last_modified=meta.get("last_modified"))
        if res.not_modified:
            await self.db.execute("UPDATE observations SET last_confirmed_at = now() WHERE id = $1", existing["id"])
            return "unchanged"
        if res.gone:
            return await self._gone(state, existing, meta)
        if res.status >= 400:
            raise RetryLater(1800, f"http {res.status}")
        doc = extract_document(res.text(), res.url)
        if doc is None or doc.lang is None:
            await self._store_minimal(state, src, key, url, "irrelevant", {"reason": "no_text_or_language"})
            return "unsupported"
        fingerprint = to_signed64(simhash(doc.text))
        group = src["independence_group"]
        tier = int(src["trust_tier"])
        syndicated_of = None
        if len(doc.text) >= 300:  # fingerprints of tiny texts are not reliable
            syndicated_of = await self._near_duplicate(state, fingerprint, exclude_key=key)
        if syndicated_of is not None:
            original = await self.db.fetchrow(
                """SELECT s.independence_group FROM observations o JOIN sources s ON s.id = o.source_id
                    WHERE o.id = $1""",
                syndicated_of,
            )
            group = original["independence_group"] if original else group
        new_meta = {**meta, "etag": res.etag, "last_modified": res.last_modified, "gone": 0, "fetched_url": res.url}
        async with self.db.transaction() as conn:
            obs_id, rev, changed = await kstore.upsert_observation(
                conn,
                kstore.NewObservation(
                    profile_id=state.profile_id,
                    source_id=src["id"],
                    kind="web_document",
                    external_id=key,
                    capture_mode="search" if mode == "search" else "crawl",
                    content=doc.text,
                    lang=doc.lang,
                    url=url,
                    published_at=doc.published_at or datetime.now(UTC),
                    meta=new_meta,
                    processing_state="pending",
                ),
            )
            await conn.execute(
                """UPDATE observations SET canonical_url = $2, title = $3, site_name = $4, simhash = $5,
                          syndicated_of = $6, date_confidence = $7, source_updated_at = coalesce($8, source_updated_at),
                          last_confirmed_at = now(), meta = meta || $9::jsonb, status = 'active'
                    WHERE id = $1""",
                obs_id, key, doc.title, doc.site_name, fingerprint, syndicated_of, doc.date_confidence,
                doc.updated_at, new_meta,
            )  # fmt: skip
            if not changed:
                return "unchanged"
            await conn.execute("UPDATE chunks SET active = false WHERE observation_id = $1 AND rev < $2", obs_id, rev)
            relevant: list[int] = []
            for ordinal, text in enumerate(chunk_text(doc.text, state.config.web.chunk_chars)):
                chunk_id = await conn.fetchval(
                    """INSERT INTO chunks
                         (observation_id, rev, ordinal, text, search_text, token_count, lang, ts_config)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8::regconfig) RETURNING id""",
                    obs_id, rev, ordinal, text, normalize(text), max(1, len(text) // 4), doc.lang,
                    kstore.TS_CONFIG.get(doc.lang, "guru_simple"),
                )  # fmt: skip
                if state.matcher.match(text):  # deterministic relevance: profile/category/entity aliases
                    relevant.append(int(chunk_id))
            if not relevant:
                await conn.execute("UPDATE observations SET processing_state = 'irrelevant' WHERE id = $1", obs_id)
                return "irrelevant"
            await conn.execute("UPDATE observations SET processing_state = 'queued' WHERE id = $1", obs_id)
            await queue.enqueue(
                conn, "llm.web_extract",
                {"profile_id": state.profile_id, "observation_id": obs_id, "chunk_ids": relevant[:20],
                 "tier": tier, "group": group},
                priority=130, idempotency_key=f"web_extract:{obs_id}:{rev}",
            )  # fmt: skip
        return "queued"

    async def _store_minimal(
        self, state: ProfileState, src: dict[str, Any], key: str, url: str, processing: str, meta: dict[str, Any]
    ) -> None:
        async with self.db.transaction() as conn:
            await kstore.upsert_observation(
                conn,
                kstore.NewObservation(
                    profile_id=state.profile_id,
                    source_id=src["id"],
                    kind="web_document",
                    external_id=key,
                    capture_mode="crawl",
                    content="",
                    url=url,
                    meta=meta,
                    processing_state=processing,
                ),
            )

    async def _near_duplicate(self, state: ProfileState, fingerprint: int, exclude_key: str) -> int | None:
        rows = await self.db.fetch(
            """SELECT id, simhash FROM observations
                WHERE profile_id = $1 AND kind = 'web_document' AND simhash IS NOT NULL AND external_id <> $2
                ORDER BY published_at NULLS LAST, id LIMIT 2000""",
            state.profile_id,
            exclude_key,
        )
        for r in rows:  # oldest first → the original wins
            if hamming(fingerprint & ((1 << 64) - 1), r["simhash"] & ((1 << 64) - 1)) <= NEAR_DUP_BITS:
                return int(r["id"])
        return None

    async def _gone(self, state: ProfileState, existing: asyncpg.Record | None, meta: dict[str, Any]) -> str:
        """Liveness: after N consecutive 404/410 the page's evidence is deactivated."""
        if existing is None:
            return "gone"
        gone = int(meta.get("gone", 0)) + 1
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE observations SET meta = meta || $2::jsonb WHERE id = $1", existing["id"], {"gone": gone}
            )
            if gone >= state.config.web.gone_after_failures:
                await conn.execute("UPDATE observations SET status = 'unreachable' WHERE id = $1", existing["id"])
                claims = await conn.fetch(
                    """UPDATE claim_evidence SET active = false, deactivated_reason = 'source_changed'
                        WHERE observation_id = $1 AND active RETURNING claim_id""",
                    existing["id"],
                )
                if claims:
                    await queue.enqueue(
                        conn,
                        "knowledge.recompute",
                        {"profile_id": state.profile_id, "claim_ids": sorted({c["claim_id"] for c in claims})},
                        priority=40,
                    )
        return "gone"

    async def handle_web_extract(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        obs = await self.db.fetchrow("SELECT * FROM observations WHERE id = $1", job.payload["observation_id"])
        if obs is None:
            return
        chunks = await self.db.fetch(
            "SELECT id, text FROM chunks WHERE id = ANY($1::bigint[]) AND active ORDER BY ordinal",
            job.payload["chunk_ids"],
        )
        published = obs["published_at"] or obs["retrieved_at"]
        sources: dict[str, SourceMessage] = {}
        authors: dict[str, str] = {}
        site = obs["site_name"] or registrable_domain(obs["url"] or "")
        for n, c in enumerate(chunks, start=1):
            sid = f"c{n}"
            sources[sid] = SourceMessage(
                sid=sid, observation_id=obs["id"], rev=obs["current_rev"], text=c["text"],
                author_tier=int(job.payload["tier"]), group=job.payload["group"], published_at=published,
                candidate=True, origin="web", chunk_id=c["id"],
            )  # fmt: skip
            authors[sid] = f"{site}, tier {job.payload['tier']}"
        if self.extraction is None:
            raise PermanentJobError("web extraction runs in the worker role")
        try:
            stats = await self.extraction.extract_and_link(state, sources, authors, web=True)
        except LLMUnavailable as exc:
            raise RetryLater(600, f"llm unavailable: {exc}") from exc
        except LLMBadOutput as exc:
            await self.db.execute("UPDATE observations SET processing_state = 'failed' WHERE id = $1", obs["id"])
            raise PermanentJobError(str(exc)) from exc
        await self.db.execute(
            "UPDATE observations SET processing_state = $2, meta = meta || $3::jsonb WHERE id = $1",
            obs["id"],
            "extracted" if stats["claims"] else "irrelevant",
            {"extraction": stats},
        )

    # ------------------------------------------------------------ discovery (W-07)
    async def handle_discover(self, job: Job) -> None:
        if self.search is None:
            return
        for state in self.registry.all():
            cfg = state.config.web.discovery
            if not cfg.enabled:
                continue
            queries = await self._discovery_queries(state)
            budget_key = hashlib.sha256(f"discover:{state.profile_id}".encode()).digest()
            for q in queries:
                async with self.db.transaction() as conn:
                    if not await usage.take(
                        conn,
                        state.profile_id,
                        budget_key,
                        "discover",
                        "day",
                        cfg.max_queries_per_day,
                        datetime.now(UTC),
                    ):
                        break
                for lang in cfg.languages:
                    try:
                        hits = await self.search.search(q, language=lang, limit=cfg.max_results_per_query)
                    except httpx.HTTPError as exc:
                        log.warning("discover.search_failed", error=str(exc)[:200])
                        return
                    async with self.db.transaction() as conn:
                        for hit in hits:
                            try:
                                _, host, _ = validate_url(hit.url)
                            except FetchBlocked:
                                continue
                            if _is_ip_literal(host):
                                continue  # search results pointing at raw IPs are never legitimate sources
                            source_id = await self.dynamic_source(conn, state, hit.url)
                            if source_id is None:
                                continue
                            await queue.enqueue(
                                conn, "web.fetch_url",
                                {"profile_id": state.profile_id, "source_id": source_id, "url": hit.url,
                                 "mode": "search"},
                                priority=170, idempotency_key=f"discover:{state.profile_id}:{url_key(hit.url)}",
                            )  # fmt: skip

    async def _discovery_queries(self, state: ProfileState) -> list[str]:
        cfg = state.config.web.discovery
        gaps = await self.db.fetch(
            """SELECT min(query_text) AS q, count(*) AS n FROM query_log
                WHERE profile_id = $1 AND answered_by = 'no_answer' AND created_at > now() - interval '7 days'
                  AND query_text IS NOT NULL
                GROUP BY query_norm_hash HAVING count(*) >= $2 ORDER BY count(*) DESC LIMIT 10""",
            state.profile_id,
            cfg.gap_min_queries,
        )
        name = state.config.profile.name
        queries = [f"{name} {g['q']}" for g in gaps]
        day = datetime.now(UTC).timetuple().tm_yday
        seeds = cfg.seed_queries
        if seeds:
            rotated = seeds[day % len(seeds) :] + seeds[: day % len(seeds)]
            queries += rotated
        return queries[: cfg.max_queries_per_day]

    # ------------------------------------------------------------ reputation (W-08)
    async def handle_reputation(self, job: Job) -> None:
        """Source tier follows agreement with knowledge verified by the team/official sources."""
        for state in self.registry.all():
            rep = state.config.web.reputation
            if not rep.enabled:
                continue
            rows = await self.db.fetch(
                """SELECT s.id, s.key, s.trust_tier,
                          count(DISTINCT c.id) FILTER (WHERE c.verification = 'verified'
                                                         AND c.verification_basis IN ('human', 'official_source')
                                                         AND e.stance = 'supports') AS agree,
                          count(DISTINCT c.id) FILTER (WHERE (c.lifecycle IN ('retracted', 'rejected')
                                                              AND e.stance = 'supports')
                                                          OR (c.verification = 'verified' AND e.stance = 'contradicts'))
                              AS disagree
                     FROM sources s
                     JOIN observations o ON o.source_id = s.id
                     JOIN claim_evidence e ON e.observation_id = o.id
                     JOIN claims c ON c.id = e.claim_id
                    WHERE s.profile_id = $1 AND s.origin = 'dynamic' AND NOT s.trust_pinned AND s.trust_tier < 4
                    GROUP BY s.id""",
                state.profile_id,
            )
            async with self.db.transaction() as conn:
                actor_id = await actors.system_actor(conn, "worker:reputation")
                for r in rows:
                    agree, disagree = int(r["agree"]), int(r["disagree"])
                    n = agree + disagree
                    score = (agree + 1) / (n + 2)  # Beta(1,1) prior
                    tier = int(r["trust_tier"])
                    new_tier = tier  # discovered sources only; 'official' (4) is never automatic
                    if n >= rep.min_claims:
                        if score >= rep.tier3_score and n >= rep.tier3_min_claims:
                            new_tier = 3
                        elif score >= rep.tier2_score:
                            new_tier = 2
                        elif score < rep.tier1_below:
                            new_tier = 1
                    await conn.execute(
                        "UPDATE sources SET reputation = $2, trust_tier = $3 WHERE id = $1",
                        r["id"],
                        {
                            "agree": agree,
                            "disagree": disagree,
                            "score": round(score, 3),
                            "at": datetime.now(UTC).isoformat(),
                        },
                        new_tier,
                    )
                    if new_tier != tier:
                        await audit.record(
                            conn,
                            actor_id=actor_id,
                            action="source.trust_auto",
                            profile_id=state.profile_id,
                            target_type="source",
                            target_id=r["key"],
                            before={"tier": tier},
                            after={"tier": new_tier, "score": round(score, 3), "n": n},
                        )

    async def handle_cleanup(self, job: Job) -> None:
        """Drop chunks of old revisions that no evidence references."""
        await self.db.execute(
            """DELETE FROM chunks c WHERE NOT c.active AND c.rev < (SELECT current_rev FROM observations o
                                                                    WHERE o.id = c.observation_id)
                 AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.chunk_id = c.id)
                 AND c.id IN (SELECT id FROM chunks WHERE NOT active LIMIT 5000)"""
        )
