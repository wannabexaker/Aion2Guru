"""Question answering pipeline (DESIGN §7).

parse → cache → aliases/routing → FTS → trigram (typos) → vector (only if lexical is weak)
→ deterministic rank → conflict expansion → extractive | list | LLM synthesis (grounded) → cache.
The LLM never runs without retrieved evidence, and its output must pass deterministic checks.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import numpy as np

from guru.core.answer import ANSWER_SCHEMA, RecordForLLM, answer_prompt, check_grounding
from guru.core.permissions import Principal
from guru.core.query import (
    Candidate,
    RankingParams,
    ScopeSpec,
    coverage,
    is_dominant,
    parse_question,
    rank,
    resolve_scope,
)
from guru.core.text import content_tokens, detect_language, normalize
from guru.db import Database
from guru.llm import embeddings as emb
from guru.llm.client import LLMBadOutput, LLMClient, LLMUnavailable
from guru.logging import get_logger
from guru.observability import metrics
from guru.services.profiles import ProfileState
from guru.store.knowledge import sha

log = get_logger(__name__)

LLMQuota = Callable[[ProfileState, Principal], Awaitable[bool]]


@dataclass(frozen=True)
class QueryRequest:
    state: ProfileState
    principal: Principal
    text: str
    guild_id: int
    channel_id: int
    allowed_channels: frozenset[int] = frozenset()  # restricted channels the asker can view
    scope_override: str | None = None
    category_override: str | None = None
    request_message_id: int | None = None
    context_text: str | None = None  # previous question when this is a follow-up


@dataclass(frozen=True)
class SourceRef:
    origin: str
    kind: str
    trust_tier: int
    evidence_at: datetime
    guild_id: int | None
    channel_id: int | None
    message_id: int | None
    url: str | None
    title: str | None

    @property
    def link(self) -> str | None:
        if self.url:
            return self.url
        if self.guild_id and self.channel_id and self.message_id:
            return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"
        return None


@dataclass
class AnswerItem:
    claim_id: int
    statement: str
    lang: str
    verification: str
    basis: str | None
    category_key: str | None
    needs_review: bool
    last_evidence_at: datetime | None
    groups: int
    max_tier: int | None
    sources: list[SourceRef] = field(default_factory=list)


@dataclass
class Answer:
    mode: str  # extractive | list | llm | conflict | no_answer | empty | faq_unavailable
    style: str  # en | el | greeklish
    question: str
    scope: str
    category_key: str | None
    items: list[AnswerItem] = field(default_factory=list)
    text: str | None = None  # LLM-synthesized text (mode 'llm')
    uncertainty: str | None = None
    off_topic: bool = False
    route: list[str] = field(default_factory=list)
    stage_ms: dict[str, float] = field(default_factory=dict)
    relevance: float = 0.0
    entity_keys: list[str] = field(default_factory=list)
    llm_tokens: tuple[int, int] = (0, 0)
    faq: dict[str, Any] | None = None  # FAQ hit (mode 'faq'): id, question, answer, banner, url
    cached: bool = False
    degraded: bool = False  # a fallback path ran (embedder/LLM down, quota) → never cached

    @property
    def answered_by(self) -> str:
        if self.cached:
            return "cache"
        return {
            "extractive": "extractive",
            "list": "extractive",
            "conflict": "extractive",
            "llm": "llm",
            "faq": "faq",
        }.get(self.mode, "no_answer")

    @property
    def claim_ids(self) -> list[int]:
        return [i.claim_id for i in self.items]

    @property
    def overall_state(self) -> str:
        states = {i.verification for i in self.items}
        if "disputed" in states or self.mode == "conflict":
            return "disputed"
        if states == {"verified"}:
            return "verified"
        if states & {"verified", "corroborated"}:
            return "corroborated"
        return "unverified"


# ---------------------------------------------------------------- (de)serialization for the cache


def answer_to_json(a: Answer) -> dict[str, Any]:
    data = asdict(a)
    for item in data["items"]:
        item["last_evidence_at"] = item["last_evidence_at"].isoformat() if item["last_evidence_at"] else None
        for s in item["sources"]:
            s["evidence_at"] = s["evidence_at"].isoformat()
    return data


def answer_from_json(d: dict[str, Any]) -> Answer:
    items = []
    for it in d.pop("items", []):
        sources = [
            SourceRef(**{**s, "evidence_at": datetime.fromisoformat(s["evidence_at"])}) for s in it.pop("sources")
        ]
        last = it.pop("last_evidence_at")
        items.append(AnswerItem(**it, last_evidence_at=datetime.fromisoformat(last) if last else None, sources=sources))
    d["llm_tokens"] = tuple(d.get("llm_tokens", (0, 0)))
    return Answer(**d, items=items)


_CLAIM_COLUMNS = """c.id, c.statement, c.search_text, c.lang, c.category_id, c.verification, c.verification_basis,
                    c.needs_review, c.last_evidence_at, c.evidence_summary"""

_FILTERS = """c.profile_id = $1 AND c.lifecycle = 'active'
              AND c.verification = ANY($2::text[])
              AND (c.is_public OR c.audience_channel_ids && $3::bigint[])
              AND ($4::text[] IS NULL OR c.origins && $4::text[])
              AND ($5::bigint[] IS NULL OR c.category_id = ANY($5::bigint[]))"""


def _tsquery(tokens: list[str]) -> str:
    safe = [t.replace("\\", "").replace("'", "''") for t in tokens if t]
    return " | ".join(f"'{t}':*" for t in safe)


class QueryService:
    def __init__(
        self,
        db: Database,
        *,
        embedder: emb.Embedder | None = None,
        llm: LLMClient | None = None,
        llm_quota: LLMQuota | None = None,
        faq: Any = None,  # FaqService
    ) -> None:
        self.db = db
        self.faq = faq
        self.embedder = embedder
        self.llm = llm
        self.llm_quota = llm_quota
        self._degraded = False

    # ------------------------------------------------------------ pipeline
    async def answer(self, req: QueryRequest) -> Answer:
        t0 = time.perf_counter()
        state = req.state
        cfg = state.config
        stage: dict[str, float] = {}

        region_values = {v.value for v in cfg.dimensions["region"].values} if "region" in cfg.dimensions else set()
        parsed = parse_question(req.text, dict(cfg.scope_tokens), set(state.category_ids), region_values)
        scope_name = req.scope_override or parsed.scope or cfg.search.default_scope
        scope = resolve_scope(scope_name, cfg.search.default_min_state)
        category_key = req.category_override or parsed.category
        lang = detect_language(parsed.text or req.text)
        answer = Answer(
            mode="no_answer", style=lang.style, question=parsed.text, scope=scope.name, category_key=category_key
        )
        retrieval_text = f"{req.context_text} {parsed.text}" if req.context_text else parsed.text
        toks = content_tokens(retrieval_text)
        if not content_tokens(parsed.text) and not req.context_text:
            answer.mode = "empty"
            return answer
        cache_key = self._cache_key(req, scope.name, category_key, retrieval_text, lang.style)
        cached = await self._cache_get(state, cache_key)
        if cached is not None:
            cached.cached = True
            cached.route = ["cache", *cached.route]
            cached.stage_ms = {"total": (time.perf_counter() - t0) * 1000}
            return cached

        # 1st rung: an approved FAQ entry answers verbatim (no retrieval ranking, no LLM).
        if self.faq is not None and not category_key:
            threshold = cfg.search.faq_match_threshold * (0.75 if scope.faq_only else 1.0)
            hit = await self.faq.search(state, parsed.text, threshold)
            if hit is not None:
                answer.mode = "faq"
                answer.route.append(f"faq:{hit['id']}")
                url = None
                if hit["thread_id"]:
                    url = f"https://discord.com/channels/{state.guild_id}/{hit['thread_id']}"
                elif hit["message_id"]:
                    url = f"https://discord.com/channels/{state.guild_id}/{hit['pub_channel']}/{hit['message_id']}"
                answer.faq = {
                    "id": hit["id"],
                    "question": hit["question"],
                    "answer": hit["answer"],
                    "banner": hit["banner"],
                    "url": url,
                }
                await self._finish(state, cache_key, answer, stage, t0)
                return answer
        if scope.faq_only:
            answer.mode = "no_answer"
            answer.route.append("faq:none")
            await self._finish(state, cache_key, answer, stage, t0)
            return answer

        # Relevance & routing: alias/keyword hits (deterministic).
        hits = state.matcher.match(retrieval_text)
        hit_categories = {h.target.target_key for h in hits if h.target.target_type == "category"}
        alias_tokens: dict[int, set[str]] = {}
        for h in hits:
            if h.target.target_type == "category":
                for cid in self._with_descendants(state, h.target.target_key):
                    alias_tokens.setdefault(cid, set()).update(h.alias.split())
        answer.entity_keys = sorted({h.target.target_key for h in hits if h.target.target_type == "entity"})
        answer.relevance = 1.0 if hits else 0.0
        answer.route.append("aliases:hit" if hits else "aliases:none")

        hard_ids: list[int] | None = None
        boost: frozenset[int] = frozenset()
        if category_key:
            hard_ids = self._with_descendants(state, category_key)
            answer.route.append(f"category:hard:{category_key}")
        elif hit_categories:
            boost = frozenset(cid for key in hit_categories for cid in self._with_descendants(state, key))
            answer.route.append("category:soft")
        stage["route"] = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        async with self.db.connection() as conn:
            candidates = await self._fts(conn, req, scope, hard_ids, toks)
            answer.route.append(f"fts:{len(candidates)}")
            for c in candidates:
                extra = alias_tokens.get(c.category_id or -1)
                if extra:
                    c.coverage = max(c.coverage, coverage(toks, c.search_text, frozenset(extra)))
            best = max((c.coverage for c in candidates), default=0.0)
            if best < cfg.search.min_coverage:
                trigram = await self._trigram(conn, req, scope, hard_ids, retrieval_text, toks)
                answer.route.append(f"trigram:{len(trigram)}")
                candidates = self._merge(candidates, trigram)
                best = max((c.coverage for c in candidates), default=0.0)
            if self.embedder is not None and (cfg.search.hybrid == "always" or best < cfg.search.min_coverage):
                self._degraded = False
                vector = await self._vector(conn, req, scope, hard_ids, retrieval_text, toks)
                answer.degraded = answer.degraded or self._degraded
                answer.route.append(f"vector:{len(vector)}")
                candidates = self._merge(candidates, vector)
            stage["retrieve"] = (time.perf_counter() - t1) * 1000

            params = RankingParams(
                rrf_k=cfg.search.rrf_k,
                state_factors=dict(cfg.search.state_factors),
                half_life_days=self._half_lives(state),
                freshness_floor=cfg.trust.freshness_floor,
                boost_categories=boost,
            )
            ranked = [
                c
                for c in rank(candidates, params, datetime.now(UTC))
                if c.coverage >= cfg.search.min_coverage or c.similarity >= cfg.search.vector_min_similarity
            ]
            if not ranked:
                answer.off_topic = not hits
                answer.mode = "no_answer"
                await self._finish(state, cache_key, answer, stage, t0)
                return answer

            dominant = is_dominant(ranked, cfg.answer.dominance_ratio)
            top = ranked[:1] if dominant else ranked[: cfg.answer.max_context_records]
            answer.items = [self._item(state, c) for c in top]
            answer.mode = "extractive" if dominant else "list"
            if any(i.verification == "disputed" for i in answer.items[:1]):
                await self._expand_conflict(conn, req, scope, answer)
            await self._attach_sources(conn, answer.items, req.allowed_channels)

        if answer.mode in ("extractive", "list") and self._wants_llm(state, answer):
            await self._synthesize(req, answer, stage)
        if answer.mode == "list":
            answer.items = answer.items[: cfg.answer.max_records_listed]
        await self._finish(state, cache_key, answer, stage, t0)
        return answer

    async def _finish(
        self, state: ProfileState, key: bytes, answer: Answer, stage: dict[str, float], t0: float
    ) -> None:
        stage["total"] = (time.perf_counter() - t0) * 1000
        answer.stage_ms = stage
        for name, ms in stage.items():
            metrics.STAGE_LATENCY.labels(stage=name).observe(ms / 1000)
        try:
            await self._cache_put(state, key, answer)
        except Exception:
            log.warning("cache.put_failed", exc_info=True)

    # ------------------------------------------------------------ LLM synthesis
    def _wants_llm(self, state: ProfileState, answer: Answer) -> bool:
        mode = state.config.answer.llm_synthesis
        if mode == "never" or self.llm is None or not self.llm.enabled("answer"):
            return False
        if mode == "always":
            return True
        if answer.mode == "list":
            return True  # several comparable records → combine them
        cross_language = answer.style != "en" and answer.items[0].lang == "en"
        return cross_language and not state.config.answer.allow_cross_language_extractive

    async def _synthesize(self, req: QueryRequest, answer: Answer, stage: dict[str, float]) -> None:
        assert self.llm is not None
        state = req.state
        cfg = state.config
        if self.llm_quota is not None and not await self.llm_quota(state, req.principal):
            answer.route.append("llm:quota")
            answer.degraded = True  # per-user outcome: must not be served to others from the cache
            return
        records = [
            RecordForLLM(
                rid=f"R{n}",
                statement=i.statement,
                verification=i.verification,
                basis=i.basis,
                category=i.category_key,
                date=i.last_evidence_at.strftime("%Y-%m-%d") if i.last_evidence_at else None,
            )
            for n, i in enumerate(answer.items, start=1)
        ]
        system, user = answer_prompt(
            cfg.profile.name, answer.question, records, answer.style,
            max_words=cfg.answer.max_answer_words, style_hint=cfg.prompts.answer_style,
            max_chars=cfg.answer.max_context_tokens * 4,
        )  # fmt: skip
        t = time.perf_counter()
        try:
            result = await self.llm.run("answer", system, user, ANSWER_SCHEMA, interactive=True)
        except (LLMUnavailable, LLMBadOutput) as exc:
            answer.route.append(f"llm:fallback:{type(exc).__name__}")
            answer.degraded = isinstance(exc, LLMUnavailable)
            return
        finally:
            stage["llm"] = (time.perf_counter() - t) * 1000
        grounding = check_grounding(result.data, records, max_chars=1800)
        answer.llm_tokens = (result.prompt_tokens, result.completion_tokens)
        if not grounding.ok:
            answer.route.append(f"llm:rejected:{grounding.failure}")
            log.info("answer.grounding_failed", failure=grounding.failure, detail=grounding.detail)
            return
        by_rid = {r.rid: item for r, item in zip(records, answer.items, strict=True)}
        cited = [by_rid[c] for c in result.data.get("cited", []) if c in by_rid]
        answer.mode = "llm"
        answer.text = str(result.data["answer"]).strip()
        answer.uncertainty = result.data.get("uncertainty")
        answer.items = cited or answer.items[:1]
        answer.route.append("llm:ok")

    # ------------------------------------------------------------ conflicts
    async def _expand_conflict(
        self, conn: asyncpg.Connection, req: QueryRequest, scope: ScopeSpec, answer: Answer
    ) -> None:
        """A disputed top record is never shown alone: present every visible side of the conflict."""
        top = answer.items[0]
        rows = await conn.fetch(
            f"""SELECT DISTINCT {_CLAIM_COLUMNS}
                  FROM conflict_members m
                  JOIN conflicts k ON k.id = m.conflict_id AND k.status IN ('open', 'acknowledged')
                  JOIN conflict_members m2 ON m2.conflict_id = k.id
                  JOIN claims c ON c.id = m2.claim_id
                 WHERE m.claim_id = $6 AND {_FILTERS}""",
            req.state.profile_id, scope.states, list(req.allowed_channels), scope.origins, None, top.claim_id,
        )  # fmt: skip
        sides = [self._item(req.state, self._candidate(r, "conflict", 1, [])) for r in rows]
        if len(sides) > 1:
            answer.items = sorted(sides, key=lambda i: (i.claim_id != top.claim_id, i.claim_id))
            answer.mode = "conflict"
            answer.route.append(f"conflict:{len(sides)}")

    # ------------------------------------------------------------ retrieval
    @staticmethod
    def _merge(a: list[Candidate], b: list[Candidate]) -> list[Candidate]:
        merged = {c.claim_id: c for c in a}
        for c in b:
            if c.claim_id in merged:
                m = merged[c.claim_id]
                m.ranks.update(c.ranks)
                m.coverage = max(m.coverage, c.coverage)
                m.similarity = max(m.similarity, c.similarity)
            else:
                merged[c.claim_id] = c
        return list(merged.values())

    def _filter_args(self, req: QueryRequest, scope: ScopeSpec, hard_ids: list[int] | None) -> list[Any]:
        return [req.state.profile_id, scope.states, list(req.allowed_channels), scope.origins, hard_ids]

    async def _fts(
        self, conn: asyncpg.Connection, req: QueryRequest, scope: ScopeSpec, hard_ids: list[int] | None,
        toks: list[str],
    ) -> list[Candidate]:  # fmt: skip
        if not toks:
            return []
        rows = await conn.fetch(
            f"""WITH q AS (SELECT to_tsquery('english', $6) || to_tsquery('greek', $6)
                                  || to_tsquery('guru_simple', $6) AS q)
                SELECT {_CLAIM_COLUMNS}, ts_rank_cd(c.tsv, q.q) AS r
                  FROM claims c, q
                 WHERE {_FILTERS} AND c.tsv @@ q.q
                 ORDER BY r DESC, c.id
                 LIMIT $7""",
            *self._filter_args(req, scope, hard_ids),
            _tsquery(toks),
            req.state.config.search.fts_k,
        )
        return [self._candidate(r, "fts", i + 1, toks) for i, r in enumerate(rows)]

    async def _trigram(
        self, conn: asyncpg.Connection, req: QueryRequest, scope: ScopeSpec, hard_ids: list[int] | None,
        text: str, toks: list[str],
    ) -> list[Candidate]:  # fmt: skip
        """Typo tolerance: per-token word similarity against claim text."""
        threshold = req.state.config.search.trigram_threshold
        query_norm = " ".join(toks) or normalize(text)
        if not query_norm:
            return []
        async with conn.transaction():
            await conn.execute("SELECT set_config('pg_trgm.word_similarity_threshold', $1, true)", str(threshold))
            rows = await conn.fetch(
                f"""SELECT {_CLAIM_COLUMNS}, word_similarity($6, c.search_text) AS r
                      FROM claims c
                     WHERE {_FILTERS} AND $6 <% c.search_text
                     ORDER BY r DESC, c.id
                     LIMIT $7""",
                *self._filter_args(req, scope, hard_ids),
                query_norm,
                req.state.config.search.fts_k,
            )
        out = []
        for i, r in enumerate(rows):
            cand = self._candidate(r, "trigram", i + 1, toks)
            cand.coverage = max(cand.coverage, float(r["r"]))
            out.append(cand)
        return out

    async def _vector(
        self, conn: asyncpg.Connection, req: QueryRequest, scope: ScopeSpec, hard_ids: list[int] | None,
        text: str, toks: list[str],
    ) -> list[Candidate]:  # fmt: skip
        """Semantic retrieval with the same filters (exact cosine; HNSW when scale requires)."""
        assert self.embedder is not None
        try:
            vector = (await self.embedder.embed([text], query=True))[0]
        except Exception as exc:
            log.warning("vector.embed_failed", error=str(exc)[:200])  # degraded mode: lexical only
            self._degraded = True
            return []
        model = await conn.fetchval("SELECT id FROM embedding_models WHERE name = $1", self.embedder.name)
        if model is None:
            return []
        rows = await conn.fetch(
            f"""SELECT {_CLAIM_COLUMNS}, 1 - (e.embedding <=> $6) AS sim
                  FROM embeddings e JOIN claims c ON c.id = e.owner_id
                 WHERE e.owner_type = 'claim' AND e.model_id = $7 AND {_FILTERS}
                 ORDER BY e.embedding <=> $6
                 LIMIT $8""",
            *self._filter_args(req, scope, hard_ids),
            np.asarray(vector, dtype=np.float32),
            model,
            req.state.config.search.vector_k,
        )
        out = []
        for i, r in enumerate(rows):
            cand = self._candidate(r, "vector", i + 1, toks)
            cand.similarity = float(r["sim"])
            out.append(cand)
        return out

    @staticmethod
    def _candidate(r: asyncpg.Record, list_name: str, position: int, toks: list[str]) -> Candidate:
        return Candidate(
            claim_id=r["id"],
            statement=r["statement"],
            search_text=r["search_text"],
            lang=r["lang"],
            category_id=r["category_id"],
            verification=r["verification"],
            basis=r["verification_basis"],
            needs_review=r["needs_review"],
            last_evidence_at=r["last_evidence_at"],
            summary=r["evidence_summary"] or {},
            ranks={list_name: position},
            coverage=coverage(toks, r["search_text"]) if toks else 0.0,
        )

    # ------------------------------------------------------------ cache
    def _cache_key(self, req: QueryRequest, scope: str, category: str | None, text: str, style: str) -> bytes:
        """Everything that can change the answer: config version, scope, text, style, visibility, models."""
        visibility = ",".join(str(c) for c in sorted(req.allowed_channels))
        models = f"{self.embedder.name if self.embedder else '-'}|{self.llm.model_of('answer') if self.llm else '-'}"
        parts = [
            str(req.state.profile_id),
            str(req.state.version),
            scope,
            category or "",
            normalize(text),
            style,
            visibility,
            models,
        ]
        return sha("\x1f".join(parts))

    async def _cache_get(self, state: ProfileState, key: bytes) -> Answer | None:
        if state.config.answer.cache_ttl_hours <= 0:
            return None
        row = await self.db.fetchrow(
            """SELECT a.response FROM answer_cache a JOIN profiles p ON p.id = a.profile_id
                WHERE a.cache_key = $1 AND a.knowledge_epoch = p.knowledge_epoch AND a.config_version = $2
                  AND a.created_at > now() - make_interval(hours => $3)""",
            key,
            state.version,
            state.config.answer.cache_ttl_hours,
        )
        if row is None:
            return None
        await self.db.execute("UPDATE answer_cache SET hits = hits + 1, last_hit_at = now() WHERE cache_key = $1", key)
        return answer_from_json(row["response"])

    async def _cache_put(self, state: ProfileState, key: bytes, answer: Answer) -> None:
        if state.config.answer.cache_ttl_hours <= 0 or answer.degraded or answer.mode in ("empty", "faq_unavailable"):
            return
        await self.db.execute(
            """INSERT INTO answer_cache (cache_key, profile_id, knowledge_epoch, config_version, response)
               SELECT $1, $2, p.knowledge_epoch, $3, $4 FROM profiles p WHERE p.id = $2
               ON CONFLICT (cache_key) DO UPDATE
                 SET knowledge_epoch = EXCLUDED.knowledge_epoch, config_version = EXCLUDED.config_version,
                     response = EXCLUDED.response, created_at = now(), hits = 0""",
            key,
            state.profile_id,
            state.version,
            answer_to_json(answer),
        )

    async def purge_cache(self, older_than: timedelta) -> int:
        status = await self.db.execute("DELETE FROM answer_cache WHERE created_at < now() - $1::interval", older_than)
        return int(status.split()[-1])

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _with_descendants(state: ProfileState, key: str) -> list[int]:
        keys = {key}
        changed = True
        flat = state.config.flat_categories()
        while changed:
            changed = False
            for cat, parent in flat:
                if parent in keys and cat.key not in keys:
                    keys.add(cat.key)
                    changed = True
        return [state.category_ids[k] for k in keys if k in state.category_ids]

    @staticmethod
    def _half_lives(state: ProfileState) -> dict[int | None, float | None]:
        out: dict[int | None, float | None] = {None: 90}
        for cat, _ in state.config.flat_categories():
            if cat.key in state.category_ids:
                out[state.category_ids[cat.key]] = cat.settings.half_life_days
        return out

    @staticmethod
    def _item(state: ProfileState, c: Candidate) -> AnswerItem:
        summary = c.summary
        return AnswerItem(
            claim_id=c.claim_id,
            statement=c.statement,
            lang=c.lang,
            verification=c.verification,
            basis=c.basis,
            category_key=state.category_keys.get(c.category_id) if c.category_id else None,
            needs_review=c.needs_review,
            last_evidence_at=c.last_evidence_at,
            groups=int(summary.get("groups_sup", 0) or 0),  # type: ignore[call-overload]
            max_tier=summary.get("max_tier"),  # type: ignore[arg-type]
        )

    @staticmethod
    async def _attach_sources(conn: asyncpg.Connection, items: list[AnswerItem], allowed: frozenset[int]) -> None:
        if not items:
            return
        rows = await conn.fetch(
            """SELECT e.claim_id, e.origin, e.trust_tier, e.evidence_at, o.kind, o.guild_id, o.channel_id,
                      o.message_id, o.url, o.title
                 FROM claim_evidence e JOIN observations o ON o.id = e.observation_id
                WHERE e.claim_id = ANY($1::bigint[]) AND e.active AND e.stance = 'supports'
                  AND (e.audience_channel_id IS NULL OR e.audience_channel_id = ANY($2::bigint[]))
                ORDER BY e.trust_tier DESC, e.evidence_at DESC""",
            [i.claim_id for i in items],
            list(allowed),
        )
        by_claim = {i.claim_id: i for i in items}
        for r in rows:
            item = by_claim[r["claim_id"]]
            if len(item.sources) < 3:
                item.sources.append(
                    SourceRef(r["origin"], r["kind"], r["trust_tier"], r["evidence_at"], r["guild_id"],
                              r["channel_id"], r["message_id"], r["url"], r["title"])
                )  # fmt: skip

    # ------------------------------------------------------------ follow-ups & logging
    async def previous_question(self, bot_message_id: int) -> str | None:
        """Follow-up support: the question answered by one of our messages (no chat history kept)."""
        return await self.db.fetchval(  # type: ignore[no-any-return]
            "SELECT query_text FROM query_log WHERE bot_message_id = $1", bot_message_id
        )

    async def log(
        self,
        req: QueryRequest,
        answer: Answer,
        *,
        user_hash: bytes,
        bot_message_id: int | None,
        answered_by: str | None = None,
    ) -> int:
        by = answered_by or answer.answered_by
        metrics.QUERIES.labels(answered_by=by).inc()
        metrics.QUERY_LATENCY.labels(answered_by=by).observe(answer.stage_ms.get("total", 0) / 1000)
        retention_days = req.state.config.retention.query_log_days
        return await self.db.fetchval(  # type: ignore[no-any-return]
            """INSERT INTO query_log (profile_id, guild_id, channel_id, user_hash, request_message_id,
                                      bot_message_id, query_text, query_norm_hash, lang, script, scope,
                                      route, answered_by, claim_ids, stage_ms, relevance, llm_prompt_tokens,
                                      llm_completion_tokens, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                       now() + make_interval(days => $19))
               RETURNING id""",
            req.state.profile_id,
            req.guild_id,
            req.channel_id,
            user_hash,
            req.request_message_id,
            bot_message_id,
            answer.question[:2000],
            sha(normalize(answer.question)),
            "el" if answer.style in ("el", "greeklish") else "en",
            answer.style,
            answer.scope,
            answer.route,
            by,
            answer.claim_ids,
            answer.stage_ms,
            answer.relevance,
            answer.llm_tokens[0] or None,
            answer.llm_tokens[1] or None,
            retention_days,
        )
