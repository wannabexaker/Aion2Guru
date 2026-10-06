"""Knowledge persistence: observations, claims, evidence, recompute (single writer of derived fields)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from guru.core.config import ProfileConfig
from guru.core.knowledge import EvidenceFacts, derive, policy_for
from guru.core.text import normalize

TS_CONFIG = {"en": "english", "el": "greek"}


def sha(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def applicability_hash(applicability: dict[str, Any]) -> bytes:
    return sha(json.dumps(applicability, sort_keys=True, separators=(",", ":")))


# ---------------------------------------------------------------- sources & observations


async def discord_source(conn: asyncpg.Connection, profile_id: int, channel_id: int) -> int:
    source_id = await conn.fetchval(
        """INSERT INTO sources (profile_id, key, kind, name, locator, independence_group, trust_tier, origin)
           VALUES ($1, $2, 'discord_channel', $2, $3, 'discord', 2, 'system')
           ON CONFLICT (profile_id, key) DO UPDATE SET enabled = true
           RETURNING id""",
        profile_id,
        f"discord:{channel_id}",
        str(channel_id),
    )
    return int(source_id)


async def system_source(conn: asyncpg.Connection, profile_id: int, key: str) -> int:
    source_id = await conn.fetchval("SELECT id FROM sources WHERE profile_id = $1 AND key = $2", profile_id, key)
    if source_id is None:
        raise LookupError(f"system source {key!r} missing for profile {profile_id}")
    return int(source_id)


@dataclass(frozen=True)
class NewObservation:
    profile_id: int
    source_id: int
    kind: str
    external_id: str
    capture_mode: str
    content: str
    lang: str | None = None
    author_actor_id: int | None = None
    author_trust_tier: int | None = None
    guild_id: int | None = None
    channel_id: int | None = None
    thread_id: int | None = None
    message_id: int | None = None
    reply_to_message_id: int | None = None
    published_at: datetime | None = None
    audience_channel_id: int | None = None
    url: str | None = None
    meta: dict[str, Any] | None = None
    processing_state: str = "pending"
    expires_at: datetime | None = None


async def upsert_observation(conn: asyncpg.Connection, o: NewObservation) -> tuple[int, int, bool]:
    """Insert or update an observation. Returns (id, rev, content_changed)."""
    digest = sha(o.content)
    row = await conn.fetchrow(
        "SELECT id, current_rev, content_hash FROM observations WHERE source_id = $1 AND external_id = $2 FOR UPDATE",
        o.source_id,
        o.external_id,
    )
    if row is None:
        obs_id = await conn.fetchval(
            """INSERT INTO observations
                 (profile_id, source_id, kind, external_id, capture_mode, guild_id, channel_id, thread_id,
                  message_id, reply_to_message_id, author_actor_id, author_trust_tier, url, content,
                  content_hash, lang, published_at, audience_channel_id, meta, processing_state, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,coalesce($17, now()),$18,$19,$20,$21)
               RETURNING id""",
            o.profile_id,
            o.source_id,
            o.kind,
            o.external_id,
            o.capture_mode,
            o.guild_id,
            o.channel_id,
            o.thread_id,
            o.message_id,
            o.reply_to_message_id,
            o.author_actor_id,
            o.author_trust_tier,
            o.url,
            o.content,
            digest,
            o.lang,
            o.published_at,
            o.audience_channel_id,
            o.meta or {},
            o.processing_state,
            o.expires_at,
        )
        await conn.execute(
            """INSERT INTO observation_revisions (observation_id, rev, content, content_hash, reason)
               VALUES ($1, 1, $2, $3, 'initial')""",
            obs_id,
            o.content,
            digest,
        )
        return int(obs_id), 1, True
    if bytes(row["content_hash"] or b"") == digest:
        return int(row["id"]), int(row["current_rev"]), False
    rev = int(row["current_rev"]) + 1
    reason = "discord_edit" if o.kind == "discord_message" else "web_change"
    await conn.execute(
        """UPDATE observations SET content = $2, content_hash = $3, current_rev = $4, source_updated_at = now(),
                  status = 'active'
            WHERE id = $1""",
        row["id"],
        o.content,
        digest,
        rev,
    )
    await conn.execute(
        """INSERT INTO observation_revisions (observation_id, rev, content, content_hash, reason)
           VALUES ($1, $2, $3, $4, $5)""",
        row["id"],
        rev,
        o.content,
        digest,
        reason,
    )
    return int(row["id"]), rev, True


# ---------------------------------------------------------------- claims


@dataclass(frozen=True)
class NewClaim:
    profile_id: int
    statement: str
    lang: str
    category_id: int | None
    claim_type: str = "fact"
    applicability: dict[str, Any] | None = None


async def find_duplicate_statement(conn: asyncpg.Connection, profile_id: int, statement: str) -> int | None:
    """Exact (normalized) duplicate among active claims. Semantic dedupe comes with embeddings (M2)."""
    found = await conn.fetchval(
        """SELECT id FROM claims
            WHERE profile_id = $1 AND kind = 'statement' AND lifecycle = 'active' AND search_text = $2
            ORDER BY id LIMIT 1""",
        profile_id,
        normalize(statement),
    )
    return None if found is None else int(found)


async def create_claim(conn: asyncpg.Connection, c: NewClaim, actor_id: int) -> int:
    applicability = c.applicability or {}
    claim_id = await conn.fetchval(
        """INSERT INTO claims (profile_id, category_id, kind, claim_type, statement, search_text, lang,
                               applicability, applicability_hash, ts_config)
           VALUES ($1, $2, 'statement', $3, $4, $5, $6, $7, $8, $9::regconfig)
           RETURNING id""",
        c.profile_id,
        c.category_id,
        c.claim_type,
        c.statement,
        normalize(c.statement),
        c.lang,
        applicability,
        applicability_hash(applicability),
        TS_CONFIG.get(c.lang, "guru_simple"),
    )
    await conn.execute(
        """INSERT INTO claim_revisions (claim_id, rev, statement, applicability, category_id, change_type, actor_id)
           VALUES ($1, 1, $2, $3, $4, 'create', $5)""",
        claim_id,
        c.statement,
        applicability,
        c.category_id,
        actor_id,
    )
    return int(claim_id)


@dataclass(frozen=True)
class NewEvidence:
    claim_id: int
    observation_id: int
    observation_rev: int
    stance: str
    quote: str | None
    quote_verified: bool
    extractor: str
    trust_tier: int
    independence_group: str
    origin: str
    evidence_at: datetime
    audience_channel_id: int | None = None
    endorsed_by: int | None = None
    endorser_tier: int | None = None
    chunk_id: int | None = None
    version_inferred: str | None = None


async def add_evidence(conn: asyncpg.Connection, e: NewEvidence) -> int | None:
    ev_id = await conn.fetchval(
        """INSERT INTO claim_evidence
             (claim_id, observation_id, observation_rev, chunk_id, stance, quote, quote_verified, extractor,
              trust_tier, independence_group, origin, evidence_at, version_inferred, audience_channel_id,
              endorsed_by, endorser_tier)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
           ON CONFLICT (claim_id, observation_id, observation_rev, stance) DO NOTHING
           RETURNING id""",
        e.claim_id,
        e.observation_id,
        e.observation_rev,
        e.chunk_id,
        e.stance,
        e.quote,
        e.quote_verified,
        e.extractor,
        e.trust_tier,
        e.independence_group,
        e.origin,
        e.evidence_at,
        e.version_inferred,
        e.audience_channel_id,
        e.endorsed_by,
        e.endorser_tier,
    )
    return None if ev_id is None else int(ev_id)


async def bump_epoch(conn: asyncpg.Connection, profile_id: int) -> int:
    epoch = await conn.fetchval(
        "UPDATE profiles SET knowledge_epoch = knowledge_epoch + 1 WHERE id = $1 RETURNING knowledge_epoch",
        profile_id,
    )
    return int(epoch)


async def recompute(conn: asyncpg.Connection, claim_id: int, cfg: ProfileConfig, now: datetime | None = None) -> str:
    """Re-derive verification/visibility/score from evidence. The only writer of derived fields.

    Returns the new verification state. Bumps the profile knowledge epoch when anything visible changed.
    """
    now = now or datetime.now(UTC)
    claim = await conn.fetchrow(
        """SELECT c.id, c.profile_id, c.lifecycle, c.verification, c.verification_basis, c.is_public,
                  c.audience_channel_ids, c.support_score, c.human_verified_by, cat.key AS category_key
             FROM claims c LEFT JOIN categories cat ON cat.id = c.category_id
            WHERE c.id = $1 FOR UPDATE OF c""",
        claim_id,
    )
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    rows = await conn.fetch(
        """SELECT stance, trust_tier, independence_group, origin, evidence_at, quote_verified, extractor,
                  endorser_tier, audience_channel_id, active
             FROM claim_evidence WHERE claim_id = $1""",
        claim_id,
    )
    open_conflict = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM conflict_members m JOIN conflicts k ON k.id = m.conflict_id
                           WHERE m.claim_id = $1 AND k.status = 'open')""",
        claim_id,
    )
    evidence = [
        EvidenceFacts(
            stance=r["stance"],
            tier=r["trust_tier"],
            group=r["independence_group"],
            origin=r["origin"],
            evidence_at=r["evidence_at"],
            quote_verified=r["quote_verified"],
            human=r["extractor"] == "human",
            endorser_tier=r["endorser_tier"],
            audience_channel_id=r["audience_channel_id"],
            active=r["active"],
        )
        for r in rows
    ]
    derived = derive(
        evidence,
        human_verified=claim["human_verified_by"] is not None,
        open_conflict=bool(open_conflict),
        policy=policy_for(cfg, claim["category_key"]),
        now=now,
    )
    lifecycle = claim["lifecycle"]
    lifecycle_reason = None
    if lifecycle == "active" and not derived.has_support and claim["human_verified_by"] is None:
        lifecycle, lifecycle_reason = "retracted", "no_evidence"
    changed = (
        derived.verification != claim["verification"]
        or derived.basis != claim["verification_basis"]
        or derived.is_public != claim["is_public"]
        or list(derived.audience_channel_ids) != list(claim["audience_channel_ids"])
        or lifecycle != claim["lifecycle"]
        or abs(derived.support_score - claim["support_score"]) > 1e-6
    )
    await conn.execute(
        """UPDATE claims
              SET verification = $2, verification_basis = $3, evidence_summary = $4, support_score = $5,
                  origins = $6, is_public = $7, audience_channel_ids = $8, last_evidence_at = $9,
                  lifecycle = $10, lifecycle_reason = coalesce($11, lifecycle_reason), updated_at = now()
            WHERE id = $1""",
        claim_id,
        derived.verification,
        derived.basis,
        derived.summary,
        derived.support_score,
        derived.origins,
        derived.is_public,
        derived.audience_channel_ids,
        derived.last_evidence_at,
        lifecycle,
        lifecycle_reason,
    )
    if changed:
        await bump_epoch(conn, claim["profile_id"])
    return derived.verification


async def claim_with_provenance(conn: asyncpg.Connection, claim_id: int) -> dict[str, Any] | None:
    claim = await conn.fetchrow(
        """SELECT c.*, cat.key AS category_key, cat.name AS category_name
             FROM claims c LEFT JOIN categories cat ON cat.id = c.category_id WHERE c.id = $1""",
        claim_id,
    )
    if claim is None:
        return None
    evidence = await conn.fetch(
        """SELECT e.id, e.stance, e.quote, e.trust_tier, e.origin, e.evidence_at, e.extractor, e.active,
                  e.deactivated_reason, e.audience_channel_id, o.kind, o.guild_id, o.channel_id, o.message_id,
                  o.url, o.title, o.status AS observation_status, a.discord_user_id AS author_id,
                  en.discord_user_id AS endorser_id
             FROM claim_evidence e
             JOIN observations o ON o.id = e.observation_id
             LEFT JOIN actors a ON a.id = o.author_actor_id
             LEFT JOIN actors en ON en.id = e.endorsed_by
            WHERE e.claim_id = $1 ORDER BY e.evidence_at DESC""",
        claim_id,
    )
    return {"claim": dict(claim), "evidence": [dict(r) for r in evidence]}
