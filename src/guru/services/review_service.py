"""Moderator review (D-27): "keep this information?" and answer ratings, votes, quorum,
labels for the learned gate, and auto-decisions only where the gate is measurably safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg

from guru.core.config import ProfileConfig
from guru.core.learned import GateModel
from guru.core.permissions import Principal
from guru.core.text import normalize
from guru.jobs import queue
from guru.observability import metrics
from guru.services.knowledge_service import NotFound, PermissionDenied
from guru.services.profiles import ProfileState
from guru.store import actors, audit
from guru.store import knowledge as kstore
from guru.store.knowledge import sha

CLAIM_DECISIONS = {"keep", "reject", "edit"}
ANSWER_DECISIONS = {"good", "bad"}


@dataclass(frozen=True)
class VoteOutcome:
    task_id: int
    closed: bool
    decision: str | None
    votes: dict[str, int]


def claim_features(statement: str, author_tier: int, prefilter_score: int, quotes: int) -> list[float]:
    """Scalar features appended to the claim embedding for the learned gate."""
    return [author_tier / 4, min(prefilter_score, 6) / 6, min(len(statement), 400) / 400, min(quotes, 4) / 4]


async def load_gate(conn: asyncpg.Connection, profile_id: int, kind: str) -> GateModel | None:
    row = await conn.fetchrow(
        "SELECT model, auto_enabled FROM learned_gates WHERE profile_id = $1 AND decision_kind = $2 AND active",
        profile_id,
        kind,
    )
    if row is None or row["model"] is None or not row["auto_enabled"]:
        return None
    return GateModel.from_json(row["model"])


async def create_task(
    conn: asyncpg.Connection,
    *,
    profile_id: int,
    kind: str,
    target_type: str,
    target_id: int,
    payload: dict[str, Any],
    priority: int = 100,
) -> int | None:
    task_id = await conn.fetchval(
        """INSERT INTO review_tasks (profile_id, kind, target_type, target_id, payload, priority)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (kind, target_type, target_id) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        profile_id,
        kind,
        target_type,
        target_id,
        payload,
        priority,
    )
    if task_id is not None:
        await queue.enqueue(
            conn, "discord.review_post", {"task_id": task_id}, priority=50, idempotency_key=f"review_post:{task_id}"
        )
    return None if task_id is None else int(task_id)


async def submit_claim_for_review(
    conn: asyncpg.Connection,
    cfg: ProfileConfig,
    profile_id: int,
    claim_id: int,
    payload: dict[str, Any],
    features: list[float] | None,
) -> str:
    """Returns 'review' (task created), 'auto_keep', 'auto_reject' or 'skipped'."""
    if not cfg.review.claim_keep.enabled:
        return "skipped"
    gate = await load_gate(conn, profile_id, "claim_keep") if cfg.review.claim_keep.auto.enabled else None
    if gate is not None and features is not None and len(features) == len(gate.weights):
        decision = gate.decide(features)
        if decision is not None:
            actor_id = await actors.system_actor(conn, "review:auto")
            await _apply_claim_decision(conn, cfg, profile_id, claim_id, decision, actor_id, None)
            await conn.execute(
                """INSERT INTO review_tasks (profile_id, kind, target_type, target_id, payload, status, closed_by,
                                            closed_at, outcome)
                   VALUES ($1, 'claim_keep', 'claim', $2, $3, 'auto', $4, now(), $5)""",
                profile_id,
                claim_id,
                payload,
                actor_id,
                {"decision": decision, "by": "learned_gate"},
            )
            await _label(conn, profile_id, "claim_keep", "claim", claim_id, int(decision == "keep"), features, "auto")
            metrics.REVIEW_DECISIONS.labels(kind="claim_keep", decision=f"auto_{decision}").inc()
            return f"auto_{decision}"
    payload = {**payload, "features": features}
    await create_task(
        conn, profile_id=profile_id, kind="claim_keep", target_type="claim", target_id=claim_id, payload=payload
    )
    return "review"


async def _label(
    conn: asyncpg.Connection,
    profile_id: int,
    kind: str,
    target_type: str,
    target_id: int,
    label: int,
    features: list[float] | None,
    source: str,
) -> None:
    await conn.execute(
        """INSERT INTO decision_labels (profile_id, decision_kind, target_type, target_id, label, features, source)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        profile_id,
        kind,
        target_type,
        target_id,
        label,
        {"x": features} if features is not None else {},
        source,
    )


async def _apply_claim_decision(
    conn: asyncpg.Connection,
    cfg: ProfileConfig,
    profile_id: int,
    claim_id: int,
    decision: str,
    actor_id: int,
    new_statement: str | None,
) -> None:
    exists = await conn.fetchval(
        "SELECT 1 FROM claims WHERE id = $1 AND profile_id = $2 FOR UPDATE", claim_id, profile_id
    )
    if exists is None:
        raise NotFound(claim_id)
    if decision in ("keep", "edit"):
        if decision == "edit" and new_statement:
            rev = await conn.fetchval(
                """UPDATE claims SET statement = $2, search_text = $3, rev = rev + 1, updated_at = now()
                    WHERE id = $1 RETURNING rev""",
                claim_id,
                new_statement,
                normalize(new_statement),
            )
            await conn.execute(
                """INSERT INTO claim_revisions (claim_id, rev, statement, applicability, category_id, change_type,
                                                reason, actor_id)
                   SELECT id, $2, statement, applicability, category_id, 'rephrase', 'moderator edit', $3
                     FROM claims WHERE id = $1""",
                claim_id,
                rev,
                actor_id,
            )
        await conn.execute(
            """UPDATE claims SET human_verified_by = $2, human_verified_at = now(), needs_review = false,
                                 lifecycle = CASE WHEN lifecycle = 'rejected' THEN 'active' ELSE lifecycle END
                WHERE id = $1""",
            claim_id,
            actor_id,
        )
    else:
        await conn.execute(
            "UPDATE claims SET lifecycle = 'rejected', lifecycle_reason = 'moderator_rejected' WHERE id = $1", claim_id
        )
    await kstore.recompute(conn, claim_id, cfg)
    await kstore.bump_epoch(conn, profile_id)
    await audit.record(
        conn,
        actor_id=actor_id,
        action=f"review.claim_{decision}",
        profile_id=profile_id,
        target_type="claim",
        target_id=claim_id,
        after={"statement": new_statement} if new_statement else None,
    )


class ReviewService:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def vote(
        self,
        state: ProfileState,
        principal: Principal,
        task_id: int,
        decision: str,
        *,
        new_statement: str | None = None,
    ) -> VoteOutcome:
        if not state.resolver.has(principal, "review.vote"):
            raise PermissionDenied("review.vote")
        async with self.db.transaction() as conn:
            task = await conn.fetchrow(
                "SELECT * FROM review_tasks WHERE id = $1 AND profile_id = $2 FOR UPDATE", task_id, state.profile_id
            )
            if task is None:
                raise NotFound(task_id)
            allowed = CLAIM_DECISIONS if task["kind"] == "claim_keep" else ANSWER_DECISIONS
            if decision not in allowed:
                raise ValueError(f"invalid decision {decision!r} for {task['kind']}")
            if task["status"] != "open":
                counts = await self._counts(conn, task_id)
                return VoteOutcome(task_id, True, (task["outcome"] or {}).get("decision"), counts)
            actor_id = await actors.discord_actor(conn, principal.user_id)
            await conn.execute(
                """INSERT INTO review_votes (task_id, actor_id, decision, note) VALUES ($1, $2, $3, $4)
                   ON CONFLICT (task_id, actor_id) DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note,
                                                                created_at = now()""",
                task_id,
                actor_id,
                decision,
                new_statement,
            )
            counts = await self._counts(conn, task_id)
            quorum = state.config.review.quorum
            # edit counts as keep for quorum; the latest edit text wins
            keepish = counts.get("keep", 0) + counts.get("edit", 0)
            decided: str | None = None
            if task["kind"] == "claim_keep":
                if keepish >= quorum:
                    decided = "edit" if new_statement and decision == "edit" else "keep"
                elif counts.get("reject", 0) >= quorum:
                    decided = "reject"
            elif counts.get(decision, 0) >= quorum:
                decided = decision
            if decided is None:
                return VoteOutcome(task_id, False, None, counts)
            await self._close(conn, state, task, decided, actor_id, new_statement)
            return VoteOutcome(task_id, True, decided, counts)

    @staticmethod
    async def _counts(conn: asyncpg.Connection, task_id: int) -> dict[str, int]:
        rows = await conn.fetch(
            "SELECT decision, count(*) AS n FROM review_votes WHERE task_id = $1 GROUP BY 1", task_id
        )
        return {r["decision"]: int(r["n"]) for r in rows}

    async def _close(
        self,
        conn: asyncpg.Connection,
        state: ProfileState,
        task: asyncpg.Record,
        decision: str,
        actor_id: int,
        new_statement: str | None,
    ) -> None:
        payload = task["payload"] or {}
        features = payload.get("features")
        if task["kind"] == "claim_keep":
            await _apply_claim_decision(
                conn, state.config, state.profile_id, task["target_id"], decision, actor_id, new_statement
            )
            label = int(decision in ("keep", "edit"))
        else:
            label = int(decision == "good")
            await conn.execute(
                "UPDATE query_log SET feedback = coalesce(feedback, $2) WHERE id = $1",
                task["target_id"],
                1 if label else -1,
            )
            await audit.record(
                conn,
                actor_id=actor_id,
                action=f"review.answer_{decision}",
                profile_id=state.profile_id,
                target_type="query",
                target_id=task["target_id"],
            )
        await _label(
            conn, state.profile_id, task["kind"], task["target_type"], task["target_id"], label, features, "human"
        )
        await conn.execute(
            """UPDATE review_tasks SET status = 'done', closed_by = $2, closed_at = now(), outcome = $3
                WHERE id = $1""",
            task["id"],
            actor_id,
            {"decision": decision, "statement": new_statement},
        )
        await queue.enqueue(
            conn,
            "discord.review_update",
            {"task_id": task["id"]},
            priority=50,
            idempotency_key=f"review_update:{task['id']}",
        )
        metrics.REVIEW_DECISIONS.labels(kind=task["kind"], decision=decision).inc()

    async def rate_answer_task(self, state: ProfileState, query_log_id: int, payload: dict[str, Any]) -> int | None:
        """Queue an answer for moderator rating (sampled by config)."""
        cfg = state.config.review.answer_rating
        if not cfg.enabled or state.mod_review_channel_id is None:
            return None
        if cfg.sample_rate < 1.0:
            bucket = int.from_bytes(sha(str(query_log_id))[:2], "big") / 65535
            if bucket > cfg.sample_rate:
                return None
        async with self.db.transaction() as conn:
            return await create_task(
                conn,
                profile_id=state.profile_id,
                kind="answer_rating",
                target_type="query",
                target_id=query_log_id,
                payload=payload,
                priority=150,
            )
