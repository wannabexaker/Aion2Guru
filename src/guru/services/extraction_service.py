"""Extraction pipeline (worker): observations → LLM (JSON schema) → validators → dedupe → evidence → review.

LLM/embedding calls never run inside a DB transaction. Claim creation is serialized per profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from guru.core.applicability import version_at
from guru.core.ingest import (
    ExtractionOutput,
    SourceMessage,
    ValidClaim,
    extraction_schema,
    validate_extraction,
)
from guru.core.learned import fit_gate
from guru.db import Database
from guru.jobs.queue import Job, PermanentJobError, RetryLater
from guru.llm import embeddings as emb
from guru.llm.client import LLMBadOutput, LLMClient, LLMUnavailable
from guru.llm.prompts import EQUIV_SCHEMA, equivalence_prompt, extraction_prompt
from guru.logging import get_logger
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.review_service import claim_features, submit_claim_for_review
from guru.store import actors, audit, usage
from guru.store import knowledge as kstore

log = get_logger(__name__)


@dataclass(frozen=True)
class Match:
    claim_id: int | None
    relation: str | None  # equivalent | contradicts | refines | None
    vector: list[float] | None


class ExtractionService:
    def __init__(
        self, db: Database, registry: ProfileRegistry, llm: LLMClient | None, embedder: emb.Embedder | None
    ) -> None:
        self.db = db
        self.registry = registry
        self.llm = llm
        self.embedder = embedder

    async def _state(self, profile_id: int) -> ProfileState:
        state = self.registry.get(profile_id)
        if state is None:
            await self.registry.reload()
            state = self.registry.get(profile_id)
        if state is None:
            raise PermanentJobError(f"profile {profile_id} not active")
        return state

    # ------------------------------------------------------------ job handlers
    async def handle_window(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        channel_id = int(job.payload["channel_id"])
        window = state.config.ingestion.window
        async with self.db.transaction() as conn:
            candidates = await conn.fetch(
                """UPDATE observations SET processing_state = 'queued'
                    WHERE id IN (SELECT id FROM observations
                                  WHERE profile_id = $1 AND channel_id = $2 AND processing_state = 'pending'
                                    AND capture_mode = 'passive'
                                  ORDER BY published_at LIMIT $3 FOR UPDATE SKIP LOCKED)
                RETURNING *""",
                state.profile_id,
                channel_id,
                window.max_messages,
            )
        if not candidates:
            return
        await self._run(state, sorted(candidates, key=lambda r: r["published_at"]), job)

    async def handle_extract(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        async with self.db.connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM observations WHERE id = ANY($1::bigint[]) AND profile_id = $2 AND content IS NOT NULL",
                job.payload["observation_ids"],
                state.profile_id,
            )
        if rows:
            await self._run(state, list(rows), job)

    async def _run(self, state: ProfileState, candidates: list[asyncpg.Record], job: Job) -> None:
        ids = [r["id"] for r in candidates]
        try:
            stats = await self.process(state, candidates)
        except LLMUnavailable as exc:
            await self._reset(ids, candidates)
            raise RetryLater(300, f"llm unavailable: {exc}") from exc
        except LLMBadOutput as exc:
            await self.db.execute(
                """UPDATE observations SET processing_state = 'failed', meta = meta || $2::jsonb
                    WHERE id = ANY($1::bigint[])""",
                ids,
                {"extraction_error": str(exc)[:300]},
            )
            raise PermanentJobError(f"bad LLM output: {exc}") from exc
        await self.db.execute(
            """UPDATE observations SET processing_state = $2, meta = meta || $3::jsonb WHERE id = ANY($1::bigint[])""",
            ids,
            "extracted" if stats["claims"] else "irrelevant",
            {"extraction": stats},
        )

    async def _reset(self, ids: list[int], rows: list[asyncpg.Record]) -> None:
        passive = [r["id"] for r in rows if r["capture_mode"] == "passive"]
        await self.db.execute(
            "UPDATE observations SET processing_state = 'pending' WHERE id = ANY($1::bigint[])", passive
        )

    # ------------------------------------------------------------ core
    async def process(self, state: ProfileState, candidates: list[asyncpg.Record]) -> dict[str, Any]:
        if self.llm is None or not self.llm.enabled("extract"):
            raise LLMUnavailable("extract task not configured")
        cfg = state.config
        first = min(r["published_at"] for r in candidates)
        last = max(r["published_at"] for r in candidates)
        async with self.db.connection() as conn:
            context = await conn.fetch(
                """SELECT * FROM observations
                    WHERE profile_id = $1 AND channel_id = $2 AND processing_state = 'context_only'
                      AND content IS NOT NULL AND published_at BETWEEN $3 AND $4
                    ORDER BY published_at""",
                state.profile_id,
                candidates[0]["channel_id"],
                first - timedelta(minutes=30),
                last,
            )
        sources, authors = self._sources(candidates, list(context), cfg.ingestion.window.max_tokens)
        prefilter_scores = {
            r["id"]: int(((r["meta"] or {}).get("prefilter") or {}).get("score", 0)) for r in candidates
        }
        return await self.extract_and_link(state, sources, authors, prefilter_scores=prefilter_scores)

    async def extract_and_link(
        self,
        state: ProfileState,
        sources: dict[str, SourceMessage],
        authors: dict[str, str],
        *,
        web: bool = False,
        prefilter_scores: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        """Shared by Discord windows and web documents: LLM → validators → link each valid claim."""
        if self.llm is None or not self.llm.enabled("extract"):
            raise LLMUnavailable("extract task not configured")
        cfg = state.config
        prompt = extraction_prompt(cfg, list(sources.values()), authors, web=web)
        keys = sorted(state.category_ids)
        result = await self.llm.run(
            "extract",
            prompt.system,
            prompt.user,
            extraction_schema(keys),
            validate=lambda d: ExtractionOutput.model_validate(d),
        )
        out = ExtractionOutput.model_validate(result.data)
        version_patterns = cfg.dimensions["game_version"].patterns if "game_version" in cfg.dimensions else []
        valid, rejected = validate_extraction(
            out,
            sources,
            set(keys),
            set(cfg.ingestion.allowed_claim_types),
            default_category="general" if "general" in state.category_ids else keys[0],
            quote_min_ratio=cfg.ingestion.quote_min_ratio,
            version_patterns=version_patterns,
        )
        extractor = f"llm:{result.model}@{prompt.version}"
        claim_ids = []
        for vc in valid:
            claim_ids.append(await self.link(state, vc, extractor, prefilter_scores))
        if rejected:
            log.info("extract.rejected", profile=state.slug, rules=[r.rule for r in rejected])
        return {
            "claims": len(valid),
            "claim_ids": claim_ids,
            "rejected": [r.rule for r in rejected],
            "extractor": extractor,
            "tokens": [result.prompt_tokens, result.completion_tokens],
        }

    @staticmethod
    def _sources(
        candidates: list[asyncpg.Record], context: list[asyncpg.Record], max_tokens: int
    ) -> tuple[dict[str, SourceMessage], dict[str, str]]:
        cand_ids = {r["id"] for r in candidates}
        rows = sorted({r["id"]: r for r in [*context, *candidates]}.values(), key=lambda r: r["published_at"])
        # Token budget (≈4 chars/token): drop the oldest context first, never candidates.
        budget = max_tokens * 4
        while sum(len(r["content"] or "") for r in rows) > budget:
            ctx = next((r for r in rows if r["id"] not in cand_ids), None)
            if ctx is None:
                break
            rows.remove(ctx)
        sources: dict[str, SourceMessage] = {}
        authors: dict[str, str] = {}
        aliases: dict[int, str] = {}
        for i, r in enumerate(rows, start=1):
            sid = f"m{i}"
            meta = r["meta"] or {}
            sources[sid] = SourceMessage(
                sid=sid,
                observation_id=r["id"],
                rev=r["current_rev"],
                text=(r["content"] or "")[:2000],
                author_tier=r["author_trust_tier"] if r["author_trust_tier"] is not None else 2,
                group=f"discord:user:{r['author_actor_id']}",
                published_at=r["published_at"],
                candidate=r["id"] in cand_ids,
                audience_channel_id=r["audience_channel_id"],
                endorsed_by=meta.get("endorsed_by"),
                endorser_tier=meta.get("endorser_tier"),
            )
            # Pseudonymous author labels: no names reach the LLM.
            author = r["author_actor_id"] or 0
            aliases.setdefault(author, f"user{len(aliases) + 1}")
            authors[sid] = f"{aliases[author]}, tier {sources[sid].author_tier}"
        return sources, authors

    async def _match(self, state: ProfileState, statement: str) -> Match:
        if self.embedder is None:
            return Match(None, None, None)
        try:
            vector = (await self.embedder.embed([statement]))[0]
        except Exception as exc:
            log.warning("embed.failed", error=str(exc)[:200])
            return Match(None, None, None)
        dedupe = state.config.search.dedupe
        async with self.db.transaction() as conn:
            model = await emb.model_id(conn, self.embedder)
            near = await emb.nearest_claims(conn, model=model, profile_id=state.profile_id, vector=vector, k=3)
        if near and near[0][1] >= dedupe.tau_high:
            return Match(near[0][0], "equivalent", vector)
        if self.llm is None or not self.llm.enabled("equivalence"):
            return Match(None, None, vector)
        for cid, sim in near:
            if sim < dedupe.tau_low:
                break
            other = await self.db.fetchval("SELECT statement FROM claims WHERE id = $1", cid)
            prompt = equivalence_prompt(state.config, other, statement)
            try:
                res = await self.llm.run("equivalence", prompt.system, prompt.user, EQUIV_SCHEMA)
            except (LLMUnavailable, LLMBadOutput):
                return Match(None, None, vector)
            relation = res.data.get("relation")
            if relation in ("equivalent", "contradicts", "refines"):
                return Match(cid, relation, vector)
        return Match(None, None, vector)

    async def link(
        self, state: ProfileState, vc: ValidClaim, extractor: str, prefilter_scores: dict[int, int] | None = None
    ) -> int:
        match = await self._match(state, vc.statement)
        cfg = state.config
        async with self.db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('claims:' || $1))", str(state.profile_id))
            actor_id = await actors.system_actor(conn, "worker:extract")
            claim_id = match.claim_id if match.relation == "equivalent" else None
            if claim_id is None:
                claim_id = await kstore.find_duplicate_statement(conn, state.profile_id, vc.statement)
            created = claim_id is None
            if claim_id is None:
                applicability = {"game_version": {"min": vc.version}} if vc.version else {}
                claim_id = await kstore.create_claim(
                    conn,
                    kstore.NewClaim(
                        profile_id=state.profile_id,
                        statement=vc.statement,
                        lang=cfg.profile.languages.canonical,
                        category_id=state.category_ids.get(vc.category_key),
                        claim_type=vc.claim_type,
                        applicability=applicability,
                    ),
                    actor_id,
                )
                if match.vector is not None and self.embedder is not None:
                    model = await emb.model_id(conn, self.embedder)
                    await emb.store(
                        conn,
                        owner_type="claim",
                        owner_id=claim_id,
                        model=model,
                        profile_id=state.profile_id,
                        text=vc.statement,
                        vector=match.vector,
                    )
                if match.claim_id is not None and match.relation in ("contradicts", "refines"):
                    await self._relate(conn, state, claim_id, match.claim_id, match.relation)
            for src, quote in vc.evidence:
                await kstore.add_evidence(
                    conn,
                    kstore.NewEvidence(
                        claim_id=claim_id,
                        observation_id=src.observation_id,
                        observation_rev=src.rev,
                        stance="supports",
                        quote=quote,
                        quote_verified=True,
                        extractor=extractor,
                        trust_tier=src.author_tier,
                        independence_group=src.group,
                        origin=src.origin,
                        evidence_at=src.published_at,
                        audience_channel_id=src.audience_channel_id,
                        endorsed_by=src.endorsed_by,
                        endorser_tier=src.endorser_tier,
                        version_inferred=vc.version or version_at(cfg, src.published_at),
                        chunk_id=src.chunk_id,
                    ),
                )
            verification = await kstore.recompute(conn, claim_id, cfg)
            await audit.record(
                conn,
                actor_id=actor_id,
                action="claim.create" if created else "claim.support",
                profile_id=state.profile_id,
                target_type="claim",
                target_id=claim_id,
                after={"statement": vc.statement, "extractor": extractor, "state": verification},
            )
            endorsed = any(src.endorser_tier is not None for src, _ in vc.evidence)
            if created and verification != "verified" and not endorsed:
                src0 = vc.evidence[0][0]
                score = (prefilter_scores or {}).get(src0.observation_id, 0)
                features = None
                if match.vector is not None:
                    features = [*match.vector, *claim_features(vc.statement, src0.author_tier, score, len(vc.evidence))]
                payload = await self._review_payload(conn, vc, claim_id, verification)
                await submit_claim_for_review(conn, cfg, state.profile_id, claim_id, payload, features)
        return claim_id

    async def _relate(
        self, conn: asyncpg.Connection, state: ProfileState, new_id: int, other_id: int, relation: str
    ) -> None:
        await conn.execute(
            """INSERT INTO claim_relations (from_claim, to_claim, type, origin) VALUES ($1, $2, $3, 'llm')
               ON CONFLICT DO NOTHING""",
            new_id,
            other_id,
            relation,
        )
        if relation != "contradicts":
            return
        conflict = await conn.fetchval(
            """SELECT k.id FROM conflicts k JOIN conflict_members m ON m.conflict_id = k.id
                WHERE m.claim_id = $1 AND k.status = 'open' LIMIT 1""",
            other_id,
        )
        if conflict is None:
            conflict = await conn.fetchval(
                "INSERT INTO conflicts (profile_id, kind) VALUES ($1, 'statement') RETURNING id", state.profile_id
            )
            await conn.execute(
                "INSERT INTO conflict_members VALUES ($1, $2) ON CONFLICT DO NOTHING", conflict, other_id
            )
        await conn.execute("INSERT INTO conflict_members VALUES ($1, $2) ON CONFLICT DO NOTHING", conflict, new_id)
        await kstore.recompute(conn, other_id, state.config)

    @staticmethod
    async def _review_payload(
        conn: asyncpg.Connection, vc: ValidClaim, claim_id: int, verification: str
    ) -> dict[str, Any]:
        quotes = []
        for src, quote in vc.evidence[:3]:
            o = await conn.fetchrow(
                """SELECT o.guild_id, o.channel_id, o.message_id, o.url, a.discord_user_id
                     FROM observations o LEFT JOIN actors a ON a.id = o.author_actor_id WHERE o.id = $1""",
                src.observation_id,
            )
            quotes.append(
                {
                    "quote": quote[:300],
                    "tier": src.author_tier,
                    "guild_id": o["guild_id"],
                    "channel_id": o["channel_id"],
                    "message_id": o["message_id"],
                    "author_id": o["discord_user_id"],
                    "url": o["url"],
                }
            )
        return {
            "claim_id": claim_id,
            "statement": vc.statement,
            "category": vc.category_key,
            "verification": verification,
            "quotes": quotes,
            "version": vc.version,
        }

    # ------------------------------------------------------------ maintenance jobs
    async def handle_recompute(self, job: Job) -> None:
        state = await self._state(job.payload["profile_id"])
        async with self.db.transaction() as conn:
            for cid in job.payload["claim_ids"]:
                await kstore.recompute(conn, int(cid), state.config)

    async def handle_embed_backfill(self, job: Job) -> None:
        """Embed active claims that have no (current) embedding. Batch, idempotent, content-hash aware."""
        if self.embedder is None:
            return
        async with self.db.transaction() as conn:
            model = await emb.model_id(conn, self.embedder)
            rows = await conn.fetch(
                """SELECT c.id, c.profile_id, c.statement FROM claims c
                    LEFT JOIN embeddings e ON e.owner_type = 'claim' AND e.owner_id = c.id AND e.model_id = $1
                   WHERE c.lifecycle = 'active'
                     AND (e.owner_id IS NULL OR e.content_hash <> sha256(convert_to(c.statement, 'UTF8')))
                   LIMIT 256""",
                model,
            )
        if not rows:
            return
        vectors = await self.embedder.embed([r["statement"] for r in rows])
        async with self.db.transaction() as conn:
            for r, v in zip(rows, vectors, strict=True):
                await emb.store(
                    conn,
                    owner_type="claim",
                    owner_id=r["id"],
                    model=model,
                    profile_id=r["profile_id"],
                    text=r["statement"],
                    vector=v,
                )
        if len(rows) == 256:
            raise RetryLater(1, "more to embed")

    async def handle_train(self, job: Job) -> None:
        """Retrain learned gates from human labels (daily)."""
        for state in self.registry.all():
            auto = state.config.review.claim_keep.auto
            async with self.db.connection() as conn:
                rows = await conn.fetch(
                    """SELECT label, features FROM decision_labels
                        WHERE profile_id = $1 AND decision_kind = 'claim_keep' AND source = 'human'
                          AND features ? 'x' ORDER BY created_at""",
                    state.profile_id,
                )
            data = [(r["features"]["x"], r["label"]) for r in rows if r["features"].get("x")]
            dims = {len(x) for x, _ in data}
            if len(dims) > 1:  # embedding model changed: keep only the newest dimensionality
                newest = len(data[-1][0])
                data = [(x, y) for x, y in data if len(x) == newest]
            gate = fit_gate(
                [x for x, _ in data], [y for _, y in data], target_precision=auto.threshold, min_labels=auto.min_labels
            )
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE learned_gates SET active = false WHERE profile_id = $1 AND decision_kind = 'claim_keep'",
                    state.profile_id,
                )
                await conn.execute(
                    """INSERT INTO learned_gates (profile_id, decision_kind, model, n_labels, auto_enabled, metrics)
                       VALUES ($1, 'claim_keep', $2, $3, $4, $5)""",
                    state.profile_id,
                    gate.to_json() if gate else None,
                    len(data),
                    bool(gate and gate.auto_enabled),
                    gate.metrics if gate else {"reason": "not enough labels"},
                )
                actor_id = await actors.system_actor(conn, "worker:learn")
                await audit.record(
                    conn,
                    actor_id=actor_id,
                    action="learn.train",
                    profile_id=state.profile_id,
                    target_type="gate",
                    target_id="claim_keep",
                    after={
                        "n_labels": len(data),
                        "auto": bool(gate and gate.auto_enabled),
                        "keep_t": gate.keep_threshold if gate else None,
                        "reject_t": gate.reject_threshold if gate else None,
                    },
                )

    async def handle_retention(self, job: Job) -> None:
        now = datetime.now(UTC)
        async with self.db.transaction() as conn:
            await conn.execute(
                """DELETE FROM observations o
                    WHERE o.processing_state = 'context_only' AND o.expires_at < $1
                      AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.observation_id = o.id)""",
                now,
            )
            # Deleted messages that produced no knowledge: drop their text (D-06 minimization).
            await conn.execute(
                """UPDATE observations o SET content = NULL, status = 'purged'
                    WHERE o.status = 'deleted' AND o.processing_state IN ('extracted', 'irrelevant', 'failed')
                      AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.observation_id = o.id AND e.active)""",
            )
            await conn.execute("DELETE FROM query_log WHERE expires_at < $1", now)
            await conn.execute(
                """DELETE FROM answer_cache a USING profiles p
                    WHERE a.profile_id = p.id
                      AND (a.knowledge_epoch <> p.knowledge_epoch OR a.created_at < $1 - interval '2 days')""",
                now,
            )
            await usage.gc(conn, now - timedelta(days=2))
