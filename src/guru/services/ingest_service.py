"""Discord ingestion (DESIGN §6, D-05/D-06): what gets stored, when extraction is queued, edit/delete sync.

Only candidates (prefilter) and explicit captures are persisted; non-candidate chatter lives in a
RAM ring buffer and is stored only as short-lived context of a candidate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from guru.core.ingest import PrefilterInput, is_question, prefilter
from guru.core.permissions import Principal
from guru.core.text import detect_language
from guru.db import Database
from guru.jobs import queue
from guru.logging import get_logger
from guru.observability import metrics
from guru.services.profiles import ProfileState
from guru.store import actors
from guru.store import knowledge as kstore

log = get_logger(__name__)


@dataclass(frozen=True)
class BufferedMessage:
    message_id: int
    channel_id: int
    author_id: int
    author_tier: int
    content: str
    created_at: datetime
    reference_id: int | None = None


class ContextBuffer:
    """Last N messages per channel, in memory only (never persisted unless a candidate needs them)."""

    def __init__(self, size: int = 50) -> None:
        self.size = size
        self._by_channel: dict[int, deque[BufferedMessage]] = {}

    def add(self, m: BufferedMessage) -> None:
        self._by_channel.setdefault(m.channel_id, deque(maxlen=self.size)).append(m)

    def get(self, channel_id: int, message_id: int) -> BufferedMessage | None:
        return next((m for m in self._by_channel.get(channel_id, ()) if m.message_id == message_id), None)

    def before(self, channel_id: int, message_id: int, n: int, within: timedelta) -> list[BufferedMessage]:
        items = list(self._by_channel.get(channel_id, ()))
        idx = next((i for i, m in enumerate(items) if m.message_id == message_id), len(items))
        anchor = items[idx].created_at if idx < len(items) else datetime.now(UTC)
        return [m for m in items[max(0, idx - n) : idx] if anchor - m.created_at <= within]

    def forget(self, channel_id: int, message_ids: set[int]) -> None:
        q = self._by_channel.get(channel_id)
        if q:
            kept = [m for m in q if m.message_id not in message_ids]
            q.clear()
            q.extend(kept)


@dataclass(frozen=True)
class CapturedMessage:
    """A specific Discord message explicitly chosen as knowledge (📌, context menu)."""

    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_tier: int
    content: str
    created_at: datetime
    restricted: bool = False  # channel not visible to @everyone → evidence audience restricted
    thread_id: int | None = None


class IngestService:
    def __init__(self, db: Database, buffer: ContextBuffer | None = None) -> None:
        self.db = db
        self.buffer = buffer or ContextBuffer()

    # ------------------------------------------------------------ passive (home channel)
    async def on_home_message(
        self,
        state: ProfileState,
        *,
        message_id: int,
        channel_id: int,
        author: Principal,
        content: str,
        created_at: datetime,
        reference_id: int | None = None,
        thread_id: int | None = None,
    ) -> str:
        tier = state.resolver.trust_tier(author)
        buffered = BufferedMessage(message_id, channel_id, author.user_id, tier, content, created_at, reference_id)
        binding = next(
            (c for c in state.config.channels if c.channel_id == channel_id and c.role in ("home", "watch")), None
        )
        if binding is not None and binding.ingest.mode in ("off", "explicit_only"):
            self.buffer.add(buffered)
            return "skipped"
        if tier < (binding.ingest.min_author_tier if binding else 1):
            metrics.INGEST_DECISIONS.labels(decision="low_tier").inc()
            self.buffer.add(buffered)  # may still be useful context for others
            return "low_tier"
        hits = state.matcher.match(content)
        entity_hits = sum(1 for h in hits if h.target.target_type == "entity")
        ref = self.buffer.get(channel_id, reference_id) if reference_id else None
        result = prefilter(
            PrefilterInput(
                text=content,
                author_tier=tier,
                alias_hits=len(hits) - entity_hits,
                entity_hits=entity_hits,
                is_reply_to_question=bool(ref and is_question(ref.content)),
                signal_bonus=binding.ingest.signal_bonus if binding else 0,
            ),
            state.config.ingestion.prefilter,
        )
        metrics.INGEST_DECISIONS.labels(decision=result.decision).inc()
        if result.decision != "drop":
            self.buffer.add(buffered)  # noise never becomes context
        if result.decision != "candidate":
            return result.decision

        cfg = state.config
        audience = channel_id if binding is not None and binding.audience == "restricted" else None
        context = self.buffer.before(channel_id, message_id, cfg.ingestion.window.context_before, timedelta(minutes=30))
        if ref and ref not in context:
            context.insert(0, ref)
        now = datetime.now(UTC)
        async with self.db.transaction() as conn:
            source_id = await kstore.discord_source(conn, state.profile_id, channel_id)
            for ctx in context:
                await self._store(
                    conn,
                    state,
                    source_id,
                    ctx,
                    "context",
                    "context_only",
                    audience,
                    expires_at=now + timedelta(days=cfg.retention.context_only_days),
                    only_new=True,
                    thread_id=thread_id,
                )
            await self._store(
                conn,
                state,
                source_id,
                buffered,
                "passive",
                "pending",
                audience,
                meta={"prefilter": {"score": result.score, "reasons": list(result.reasons)}},
                thread_id=thread_id,
            )
            idle = cfg.ingestion.window.idle_seconds
            bucket = int(now.timestamp() // max(idle, 1))
            await queue.enqueue(
                conn,
                "llm.window",
                {"profile_id": state.profile_id, "channel_id": channel_id},
                priority=120,
                idempotency_key=f"window:{channel_id}:{bucket}",
                run_after=now + timedelta(seconds=idle),
            )
        return "candidate"

    async def _store(
        self,
        conn: asyncpg.Connection,
        state: ProfileState,
        source_id: int,
        m: BufferedMessage,
        capture_mode: str,
        processing_state: str,
        audience: int | None,
        *,
        meta: dict[str, object] | None = None,
        expires_at: datetime | None = None,
        only_new: bool = False,
        thread_id: int | None = None,
    ) -> tuple[int, int]:
        existing = await conn.fetchval(
            "SELECT id FROM observations WHERE source_id = $1 AND external_id = $2", source_id, str(m.message_id)
        )
        if existing is not None and only_new:
            return int(existing), 1
        author_actor = await actors.discord_actor(conn, m.author_id)
        obs_id, rev, _ = await kstore.upsert_observation(
            conn,
            kstore.NewObservation(
                profile_id=state.profile_id,
                source_id=source_id,
                kind="discord_message",
                external_id=str(m.message_id),
                capture_mode=capture_mode,
                content=m.content,
                lang=detect_language(m.content).lang,
                author_actor_id=author_actor,
                author_trust_tier=m.author_tier,
                guild_id=state.guild_id,
                channel_id=m.channel_id,
                thread_id=thread_id,
                message_id=m.message_id,
                reply_to_message_id=m.reference_id,
                published_at=m.created_at,
                audience_channel_id=audience,
                meta=meta,
                processing_state=processing_state,
                expires_at=expires_at,
            ),
        )
        if existing is not None and capture_mode != "context":
            # A context row becoming a candidate: promote it.
            await conn.execute(
                """UPDATE observations SET capture_mode = $2, processing_state = $3, expires_at = NULL,
                          meta = meta || $4::jsonb WHERE id = $1 AND processing_state = 'context_only'""",
                obs_id,
                capture_mode,
                processing_state,
                meta or {},
            )
        return obs_id, rev

    # ------------------------------------------------------------ explicit capture
    async def capture_explicit(
        self, state: ProfileState, endorser: Principal, msg: CapturedMessage, *, reason: str
    ) -> int:
        """📌 / "Add to knowledge" / teach-by-mention. Requires kb.ingest. Returns the observation id."""
        if not state.resolver.has(endorser, "kb.ingest"):
            from guru.services.knowledge_service import PermissionDenied

            raise PermissionDenied("kb.ingest")
        endorser_tier = state.resolver.trust_tier(endorser)
        buffered = BufferedMessage(
            msg.message_id, msg.channel_id, msg.author_id, msg.author_tier, msg.content, msg.created_at
        )
        async with self.db.transaction() as conn:
            endorser_actor = await actors.discord_actor(conn, endorser.user_id)
            source_id = await kstore.discord_source(conn, state.profile_id, msg.channel_id)
            obs_id, rev = await self._store(
                conn,
                state,
                source_id,
                buffered,
                "explicit",
                "queued",
                msg.channel_id if msg.restricted else None,
                meta={"endorsed_by": endorser_actor, "endorser_tier": endorser_tier, "reason": reason},
                thread_id=msg.thread_id,
            )
            await conn.execute(
                """UPDATE observations SET capture_mode = 'explicit', processing_state = 'queued', expires_at = NULL,
                          meta = meta || $2::jsonb WHERE id = $1""",
                obs_id,
                {"endorsed_by": endorser_actor, "endorser_tier": endorser_tier, "reason": reason},
            )
            await queue.enqueue(
                conn,
                "llm.extract",
                {"profile_id": state.profile_id, "observation_ids": [obs_id]},
                priority=20,
                idempotency_key=f"extract:{obs_id}:{rev}",
            )
        log.info("ingest.explicit", profile=state.slug, observation=obs_id, reason=reason)
        return obs_id

    # ------------------------------------------------------------ edits & deletes
    async def on_edit(self, channel_id: int, message_id: int, content: str) -> bool:
        """New revision; evidence from the old revision is deactivated and extraction re-queued."""
        async with self.db.transaction() as conn:
            row = await conn.fetchrow(
                """SELECT o.id, o.profile_id, o.source_id, o.current_rev, o.capture_mode, o.processing_state
                     FROM observations o WHERE o.message_id = $1 AND o.channel_id = $2 FOR UPDATE""",
                message_id,
                channel_id,
            )
            if row is None:
                return False
            obs = await conn.fetchrow("SELECT * FROM observations WHERE id = $1", row["id"])
            _, rev, changed = await kstore.upsert_observation(
                conn,
                kstore.NewObservation(
                    profile_id=obs["profile_id"],
                    source_id=obs["source_id"],
                    kind=obs["kind"],
                    external_id=obs["external_id"],
                    capture_mode=obs["capture_mode"],
                    content=content,
                ),
            )
            if not changed:
                return False
            claim_ids = [
                r["claim_id"]
                for r in await conn.fetch(
                    """UPDATE claim_evidence SET active = false, deactivated_reason = 'source_edited'
                        WHERE observation_id = $1 AND observation_rev < $2 AND active RETURNING claim_id""",
                    row["id"],
                    rev,
                )
            ]
            if row["capture_mode"] in ("passive", "explicit"):
                await conn.execute("UPDATE observations SET processing_state = 'queued' WHERE id = $1", row["id"])
                await queue.enqueue(
                    conn,
                    "llm.extract",
                    {"profile_id": row["profile_id"], "observation_ids": [row["id"]]},
                    priority=60,
                    idempotency_key=f"extract:{row['id']}:{rev}",
                )
            if claim_ids:
                await queue.enqueue(
                    conn,
                    "knowledge.recompute",
                    {"profile_id": row["profile_id"], "claim_ids": sorted(set(claim_ids))},
                    priority=40,
                )
        return True

    async def on_delete(self, channel_id: int, message_ids: list[int], purge: bool) -> int:
        """D-06: keep_knowledge → the observation is marked deleted, evidence stays; purge → evidence removed."""
        self.buffer.forget(channel_id, set(message_ids))
        async with self.db.transaction() as conn:
            rows = await conn.fetch(
                """UPDATE observations SET status = 'deleted'
                    WHERE channel_id = $1 AND message_id = ANY($2::bigint[]) AND status = 'active'
                RETURNING id, profile_id""",
                channel_id,
                message_ids,
            )
            if not rows:
                return 0
            ids = [r["id"] for r in rows]
            if purge:
                claim_rows = await conn.fetch(
                    """UPDATE claim_evidence SET active = false, deactivated_reason = 'source_deleted', quote = NULL
                        WHERE observation_id = ANY($1::bigint[]) AND active RETURNING claim_id""",
                    ids,
                )
                await conn.execute(
                    """UPDATE observations SET status = 'purged', content = NULL WHERE id = ANY($1::bigint[])""", ids
                )
                await conn.execute(
                    """UPDATE observation_revisions SET content = NULL WHERE observation_id = ANY($1::bigint[])""",
                    ids,
                )
                claim_ids = sorted({c["claim_id"] for c in claim_rows})
                if claim_ids:
                    owners = await conn.fetch(
                        "SELECT profile_id, array_agg(id) AS ids FROM claims WHERE id = ANY($1::bigint[]) GROUP BY 1",
                        claim_ids,
                    )
                    for o in owners:
                        await queue.enqueue(
                            conn,
                            "knowledge.recompute",
                            {"profile_id": o["profile_id"], "claim_ids": sorted(o["ids"])},
                            priority=40,
                        )
            # Context-only rows of deleted messages have no value: drop their content now.
            await conn.execute(
                """UPDATE observations SET content = NULL, status = 'purged'
                    WHERE id = ANY($1::bigint[]) AND processing_state = 'context_only'""",
                ids,
            )
        return len(rows)

    async def forget_user(self, profile_id: int, user_id: int) -> int:
        """GDPR erasure (D-06): purge everything this user wrote; knowledge survives only if supported elsewhere."""
        async with self.db.transaction() as conn:
            actor_id = await conn.fetchval("SELECT id FROM actors WHERE discord_user_id = $1", user_id)
            if actor_id is None:
                return 0
            obs = [r["id"] for r in await conn.fetch(
                """UPDATE observations SET content = NULL, status = 'purged', meta = '{}'::jsonb
                    WHERE profile_id = $1 AND author_actor_id = $2 AND status <> 'purged' RETURNING id""",
                profile_id, actor_id,
            )]  # fmt: skip
            if obs:
                await conn.execute(
                    "UPDATE observation_revisions SET content = NULL WHERE observation_id = ANY($1::bigint[])", obs
                )
                claims = await conn.fetch(
                    """UPDATE claim_evidence SET active = false, deactivated_reason = 'purged', quote = NULL
                        WHERE observation_id = ANY($1::bigint[]) AND active RETURNING claim_id""",
                    obs,
                )
                if claims:
                    await queue.enqueue(
                        conn,
                        "knowledge.recompute",
                        {"profile_id": profile_id, "claim_ids": sorted({c["claim_id"] for c in claims})},
                        priority=30,
                    )
            await conn.execute(
                """INSERT INTO audit_log (actor_id, action, profile_id, target_type, target_id, after)
                   VALUES ($1, 'user.forget', $2, 'user', $3, $4)""",
                actor_id, profile_id, "self", {"observations": len(obs)},
            )  # fmt: skip
        return len(obs)
