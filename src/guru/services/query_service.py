"""Question answering pipeline (DESIGN §7). M1: deterministic only — no embeddings, no LLM.

parse → relevance (aliases) → scope/category routing → FTS (+ trigram for typos) → rank → extractive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

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
from guru.observability import metrics
from guru.services.profiles import ProfileState
from guru.store.knowledge import sha


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
    mode: str  # 'extractive' | 'list' | 'no_answer' | 'empty' | 'faq_unavailable'
    style: str  # 'en' | 'el' | 'greeklish'
    question: str
    scope: str
    category_key: str | None
    items: list[AnswerItem] = field(default_factory=list)
    off_topic: bool = False
    route: list[str] = field(default_factory=list)
    stage_ms: dict[str, float] = field(default_factory=dict)
    relevance: float = 0.0
    entity_keys: list[str] = field(default_factory=list)

    @property
    def answered_by(self) -> str:
        return {"extractive": "extractive", "list": "extractive"}.get(self.mode, "no_answer")

    @property
    def claim_ids(self) -> list[int]:
        return [i.claim_id for i in self.items]


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
    def __init__(self, db: Database) -> None:
        self.db = db

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

        toks = content_tokens(parsed.text)
        if not toks:
            answer.mode = "empty"
            return answer

        # Relevance: alias/keyword hits (deterministic). Gray zone → local retrieval decides.
        hits = state.matcher.match(parsed.text)
        hit_categories = {h.target.target_key for h in hits if h.target.target_type == "category"}
        # Tokens that resolved to a category alias, per category id (incl. descendants).
        alias_tokens: dict[int, set[str]] = {}
        for h in hits:
            if h.target.target_type == "category":
                for cid in self._with_descendants(state, h.target.target_key):
                    alias_tokens.setdefault(cid, set()).update(h.alias.split())
        answer.entity_keys = sorted({h.target.target_key for h in hits if h.target.target_type == "entity"})
        answer.relevance = 1.0 if hits else 0.0
        answer.route.append("aliases:hit" if hits else "aliases:none")
        stage["route"] = (time.perf_counter() - t0) * 1000

        if scope.faq_only:
            answer.mode = "faq_unavailable"  # FAQ store arrives in M5
            answer.route.append("faq")
            return answer

        hard_ids: list[int] | None = None
        boost: frozenset[int] = frozenset()
        if category_key:
            hard_ids = self._with_descendants(state, category_key)
            answer.route.append(f"category:hard:{category_key}")
        elif hit_categories:
            boost = frozenset(cid for key in hit_categories for cid in self._with_descendants(state, key))
            answer.route.append("category:soft")

        t1 = time.perf_counter()
        async with self.db.connection() as conn:
            candidates = await self._fts(conn, req, scope, hard_ids, toks)
            answer.route.append(f"fts:{len(candidates)}")
            best_cov = max(
                (
                    coverage(toks, c.search_text, frozenset(alias_tokens.get(c.category_id or -1, ())))
                    for c in candidates
                ),
                default=0.0,
            )
            if best_cov < cfg.search.min_coverage:
                trigram = await self._trigram(conn, req, scope, hard_ids, parsed.text, toks)
                answer.route.append(f"trigram:{len(trigram)}")
                merged = {c.claim_id: c for c in candidates}
                for c in trigram:
                    if c.claim_id in merged:
                        merged[c.claim_id].ranks.update(c.ranks)
                        merged[c.claim_id].coverage = max(merged[c.claim_id].coverage, c.coverage)
                    else:
                        merged[c.claim_id] = c
                candidates = list(merged.values())
            stage["retrieve"] = (time.perf_counter() - t1) * 1000

            for c in candidates:
                extra = alias_tokens.get(c.category_id or -1)
                if extra:
                    c.coverage = max(c.coverage, coverage(toks, c.search_text, frozenset(extra)))
            params = RankingParams(
                rrf_k=cfg.search.rrf_k,
                state_factors=dict(cfg.search.state_factors),
                half_life_days=self._half_lives(state),
                freshness_floor=cfg.trust.freshness_floor,
                boost_categories=boost,
            )
            ranked = [c for c in rank(candidates, params, datetime.now(UTC)) if c.coverage >= cfg.search.min_coverage]
            if not ranked:
                answer.off_topic = not hits
                answer.mode = "no_answer"
                stage["total"] = (time.perf_counter() - t0) * 1000
                answer.stage_ms = stage
                return answer

            dominant = is_dominant(ranked, cfg.answer.dominance_ratio)
            top = ranked[:1] if dominant else ranked[: cfg.answer.max_records_listed]
            answer.mode = "extractive" if dominant else "list"
            answer.items = [self._item(state, c) for c in top]
            await self._attach_sources(conn, answer.items, req.allowed_channels)
        stage["total"] = (time.perf_counter() - t0) * 1000
        answer.stage_ms = stage
        return answer

    # ------------------------------------------------------------ retrieval
    def _filter_args(self, req: QueryRequest, scope: ScopeSpec, hard_ids: list[int] | None) -> list[Any]:
        return [req.state.profile_id, scope.states, list(req.allowed_channels), scope.origins, hard_ids]

    async def _fts(
        self,
        conn: asyncpg.Connection,
        req: QueryRequest,
        scope: ScopeSpec,
        hard_ids: list[int] | None,
        toks: list[str],
    ) -> list[Candidate]:
        q = _tsquery(toks)
        rows = await conn.fetch(
            f"""WITH q AS (SELECT to_tsquery('english', $6) || to_tsquery('greek', $6)
                                  || to_tsquery('guru_simple', $6) AS q)
                SELECT {_CLAIM_COLUMNS}, ts_rank_cd(c.tsv, q.q) AS r
                  FROM claims c, q
                 WHERE {_FILTERS} AND c.tsv @@ q.q
                 ORDER BY r DESC, c.id
                 LIMIT $7""",
            *self._filter_args(req, scope, hard_ids),
            q,
            req.state.config.search.fts_k,
        )
        return [self._candidate(r, "fts", i + 1, toks) for i, r in enumerate(rows)]

    async def _trigram(
        self,
        conn: asyncpg.Connection,
        req: QueryRequest,
        scope: ScopeSpec,
        hard_ids: list[int] | None,
        text: str,
        toks: list[str],
    ) -> list[Candidate]:
        """Typo tolerance: per-token word similarity against claim text."""
        threshold = req.state.config.search.trigram_threshold
        query_norm = " ".join(toks) or normalize(text)
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
            cand.coverage = max(cand.coverage, float(r["r"]))  # similarity stands in for exact token coverage
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
            coverage=coverage(toks, r["search_text"]),
        )

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
                    SourceRef(
                        r["origin"],
                        r["kind"],
                        r["trust_tier"],
                        r["evidence_at"],
                        r["guild_id"],
                        r["channel_id"],
                        r["message_id"],
                        r["url"],
                        r["title"],
                    )
                )

    # ------------------------------------------------------------ logging
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
                                      route, answered_by, claim_ids, stage_ms, relevance, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                       now() + make_interval(days => $17))
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
            retention_days,
        )
