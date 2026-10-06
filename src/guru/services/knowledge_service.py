"""Knowledge use cases with permission checks and audit: add, verify, retract, show."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from guru.core.permissions import Principal
from guru.core.text import detect_language
from guru.db import Database
from guru.services.profiles import ProfileState
from guru.store import actors, audit
from guru.store import knowledge as kstore


class PermissionDenied(Exception):
    def __init__(self, capability: str) -> None:
        super().__init__(capability)
        self.capability = capability


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class AddResult:
    claim_id: int
    created: bool
    verification: str


def infer_category(state: ProfileState, text: str, explicit: str | None = None) -> str | None:
    """Explicit key > first category alias hit > 'general' if it exists."""
    if explicit:
        return explicit if explicit in state.category_ids else None
    for hit in state.matcher.match(text):
        if hit.target.target_type == "category" and hit.target.target_key in state.category_ids:
            return hit.target.target_key
    return "general" if "general" in state.category_ids else None


class KnowledgeService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _require(state: ProfileState, principal: Principal, capability: str) -> None:
        if not state.resolver.has(principal, capability):
            raise PermissionDenied(capability)

    async def add_manual(
        self,
        state: ProfileState,
        principal: Principal,
        statement: str,
        *,
        category_key: str | None = None,
        verify: bool = False,
        channel_id: int | None = None,
        interaction_id: int | None = None,
    ) -> AddResult:
        """Explicit knowledge entry by a human (/kb add). The entering member endorses it."""
        self._require(state, principal, "kb.ingest")
        if verify:
            self._require(state, principal, "kb.verify")
        statement = statement.strip()
        if not 5 <= len(statement) <= 1500:
            raise ValueError("statement must be 5–1500 characters")
        cat_key = infer_category(state, statement, category_key)
        if category_key and cat_key is None:
            raise ValueError(f"unknown category {category_key!r}")
        tier = state.resolver.trust_tier(principal)
        lang = detect_language(statement).lang
        now = datetime.now(UTC)
        async with self.db.transaction() as conn:
            actor_id = await actors.discord_actor(conn, principal.user_id)
            source_id = await kstore.system_source(conn, state.profile_id, "manual")
            obs_id, rev, _ = await kstore.upsert_observation(
                conn,
                kstore.NewObservation(
                    profile_id=state.profile_id,
                    source_id=source_id,
                    kind="manual",
                    external_id=f"manual:{interaction_id or now.timestamp()}:{principal.user_id}",
                    capture_mode="manual",
                    content=statement,
                    lang=lang,
                    author_actor_id=actor_id,
                    author_trust_tier=tier,
                    guild_id=state.guild_id,
                    channel_id=channel_id,
                    published_at=now,
                    processing_state="extracted",
                ),
            )
            existing = await kstore.find_duplicate_statement(conn, state.profile_id, statement)
            claim_id = existing or await kstore.create_claim(
                conn,
                kstore.NewClaim(
                    profile_id=state.profile_id,
                    statement=statement,
                    lang=lang,
                    category_id=state.category_ids.get(cat_key) if cat_key else None,
                ),
                actor_id,
            )
            await kstore.add_evidence(
                conn,
                kstore.NewEvidence(
                    claim_id=claim_id,
                    observation_id=obs_id,
                    observation_rev=rev,
                    stance="supports",
                    quote=statement,
                    quote_verified=True,
                    extractor="human",
                    trust_tier=tier,
                    independence_group=f"discord:user:{principal.user_id}",
                    origin="manual",
                    evidence_at=now,
                    endorsed_by=actor_id,
                    endorser_tier=tier,
                ),
            )
            if verify:
                await conn.execute(
                    "UPDATE claims SET human_verified_by = $2, human_verified_at = now() WHERE id = $1",
                    claim_id,
                    actor_id,
                )
            verification = await kstore.recompute(conn, claim_id, state.config)
            await audit.record(
                conn,
                actor_id=actor_id,
                action="claim.create" if existing is None else "claim.support",
                profile_id=state.profile_id,
                target_type="claim",
                target_id=claim_id,
                after={"statement": statement, "category": cat_key, "verified": verify, "state": verification},
            )
        return AddResult(claim_id, existing is None, verification)

    async def verify(self, state: ProfileState, principal: Principal, claim_id: int) -> str:
        self._require(state, principal, "kb.verify")
        async with self.db.transaction() as conn:
            actor_id = await actors.discord_actor(conn, principal.user_id)
            row = await conn.fetchrow(
                """UPDATE claims SET human_verified_by = $3, human_verified_at = now(), needs_review = false
                    WHERE id = $1 AND profile_id = $2 AND lifecycle = 'active'
                RETURNING verification""",
                claim_id,
                state.profile_id,
                actor_id,
            )
            if row is None:
                raise NotFound(claim_id)
            state_after = await kstore.recompute(conn, claim_id, state.config)
            await audit.record(
                conn,
                actor_id=actor_id,
                action="claim.verify",
                profile_id=state.profile_id,
                target_type="claim",
                target_id=claim_id,
                before={"verification": row["verification"]},
                after={"verification": state_after},
            )
        return state_after

    async def set_lifecycle(
        self, state: ProfileState, principal: Principal, claim_id: int, lifecycle: str, reason: str
    ) -> None:
        """retract (was wrong) / obsolete (no longer applies) / reject (not knowledge)."""
        if lifecycle not in ("retracted", "obsolete", "rejected", "active"):
            raise ValueError(lifecycle)
        self._require(state, principal, "kb.edit")
        async with self.db.transaction() as conn:
            actor_id = await actors.discord_actor(conn, principal.user_id)
            before = await conn.fetchval(
                "SELECT lifecycle FROM claims WHERE id = $1 AND profile_id = $2 FOR UPDATE", claim_id, state.profile_id
            )
            if before is None:
                raise NotFound(claim_id)
            await conn.execute(
                "UPDATE claims SET lifecycle = $2, lifecycle_reason = $3, updated_at = now() WHERE id = $1",
                claim_id,
                lifecycle,
                reason,
            )
            await kstore.bump_epoch(conn, state.profile_id)
            await audit.record(
                conn,
                actor_id=actor_id,
                action=f"claim.{lifecycle}",
                profile_id=state.profile_id,
                target_type="claim",
                target_id=claim_id,
                before={"lifecycle": before},
                after={"lifecycle": lifecycle},
                reason=reason,
            )

    async def show(self, state: ProfileState, claim_id: int, allowed_channels: set[int]) -> dict[str, Any]:
        async with self.db.connection() as conn:
            data = await kstore.claim_with_provenance(conn, claim_id)
        if data is None or data["claim"]["profile_id"] != state.profile_id:
            raise NotFound(claim_id)
        claim = data["claim"]
        if not claim["is_public"] and not set(claim["audience_channel_ids"]) & allowed_channels:
            raise NotFound(claim_id)  # do not even confirm existence
        data["evidence"] = [
            e
            for e in data["evidence"]
            if e["audience_channel_id"] is None or e["audience_channel_id"] in allowed_channels
        ]
        return data
