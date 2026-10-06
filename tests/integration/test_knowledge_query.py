from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from guru.core.permissions import Principal
from guru.db import Database
from guru.services.knowledge_service import KnowledgeService, NotFound, PermissionDenied
from guru.services.query_service import QueryRequest, QueryService
from guru.services.ratelimit_service import RateLimiter
from guru.store import actors, audit
from guru.store import knowledge as kstore
from tests.integration.helpers import (
    GUILD,
    HOME,
    ROLE_MEMBER,
    ROLE_MOD,
    ROLE_TRUSTED,
    SECRET,
    default_setup,
    make_profile,
)

pytestmark = pytest.mark.db

MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))
TRUSTED = Principal(user_id=2, role_ids=frozenset({ROLE_TRUSTED}))
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
STRANGER = Principal(user_id=4)


def ask(state, text: str, who: Principal = MEMBER, allowed: frozenset[int] = frozenset(), **kw) -> QueryRequest:  # type: ignore[no-untyped-def]
    return QueryRequest(
        state=state, principal=who, text=text, guild_id=GUILD, channel_id=HOME, allowed_channels=allowed, **kw
    )


async def test_add_manual_by_trusted_is_verified_and_answerable(db: Database) -> None:
    state = await make_profile(db)
    ks = KnowledgeService(db)
    res = await ks.add_manual(state, TRUSTED, "The Fire Temple boss respawns every 4 hours.", interaction_id=1)
    assert res.created and res.verification == "verified"  # trusted explicit capture (D-11)
    assert await db.fetchval("SELECT category_id IS NOT NULL FROM claims WHERE id = $1", res.claim_id)

    qs = QueryService(db)
    ans = await qs.answer(ask(state, "when does the fire temple boss respawn?"))
    assert ans.mode == "extractive" and ans.items[0].claim_id == res.claim_id
    assert ans.items[0].verification == "verified"
    assert ans.items[0].sources and ans.items[0].sources[0].origin == "manual"


async def test_greek_and_greeklish_questions_find_english_knowledge(db: Database) -> None:
    state = await make_profile(db)
    ks = KnowledgeService(db)
    await ks.add_manual(state, TRUSTED, "Gladiator is the best tank class for dungeons.", interaction_id=1)
    qs = QueryService(db)
    for q in ("poia klasi einai kalh gia tank?", "Ποια κλάση είναι καλή για tank;", "best tank class"):
        ans = await qs.answer(ask(state, q))
        assert ans.items, q
    assert (await qs.answer(ask(state, "poia klasi einai kalh gia tank?"))).style == "greeklish"


async def test_member_cannot_add_and_duplicates_merge(db: Database) -> None:
    state = await make_profile(db)
    ks = KnowledgeService(db)
    with pytest.raises(PermissionDenied):
        await ks.add_manual(state, MEMBER, "Some fact about the game", interaction_id=1)
    first = await ks.add_manual(state, MOD, "Enchanting above +10 can fail.", interaction_id=2)
    second = await ks.add_manual(state, TRUSTED, "enchanting ABOVE +10 can fail", interaction_id=3)
    assert second.claim_id == first.claim_id and not second.created
    assert await db.fetchval("SELECT count(*) FROM claim_evidence WHERE claim_id = $1", first.claim_id) == 2


async def test_unverified_trusted_threshold_and_mod_verify(db: Database) -> None:
    def contributors_are_members(c: dict[str, Any]) -> None:
        default_setup(c)
        c["role_groups"]["contributors"] = [ROLE_MEMBER]

    state = await make_profile(db, contributors_are_members)
    ks = KnowledgeService(db)
    # A contributor at community tier → unverified until a moderator verifies it.
    res = await ks.add_manual(state, MEMBER, "Kinah can be earned from daily quests.", interaction_id=1)
    assert res.verification == "unverified"
    assert await ks.verify(state, MOD, res.claim_id) == "verified"
    async with db.connection() as conn:
        actions = [r["action"] for r in await audit.query(conn, target_type="claim")]
    assert actions == ["claim.verify", "claim.create"]


async def test_retracted_claims_never_answer(db: Database) -> None:
    state = await make_profile(db)
    ks = KnowledgeService(db)
    res = await ks.add_manual(state, MOD, "Manastones drop from field bosses.", interaction_id=1)
    qs = QueryService(db)
    assert (await qs.answer(ask(state, "where do manastones drop"))).items
    with pytest.raises(PermissionDenied):
        await ks.set_lifecycle(state, TRUSTED, res.claim_id, "retracted", "wrong")
    await ks.set_lifecycle(state, MOD, res.claim_id, "retracted", "wrong")
    assert not (await qs.answer(ask(state, "where do manastones drop"))).items


async def test_off_topic_and_scope_and_hard_category(db: Database) -> None:
    state = await make_profile(db)
    ks = KnowledgeService(db)
    await ks.add_manual(
        state, MOD, "Dungeon entry limits reset daily at 09:00 server time.", category_key="content", interaction_id=1
    )
    qs = QueryService(db)
    off = await qs.answer(ask(state, "what is the capital of France"))
    assert off.mode == "no_answer" and off.off_topic
    assert (await qs.answer(ask(state, "the of and"))).mode == "empty"
    # explicit category that does not hold the fact → nothing (hard filter)
    assert not (await qs.answer(ask(state, "#economy dungeon entry reset"))).items
    assert (await qs.answer(ask(state, "#content dungeon entry reset"))).items
    # web scope: knowledge is manual → excluded; internal includes it
    assert not (await qs.answer(ask(state, "web: dungeon entry reset"))).items
    assert (await qs.answer(ask(state, "internal: dungeon entry reset"))).items
    assert (await qs.answer(ask(state, "faq: dungeon entry reset"))).mode == "faq_unavailable"


async def test_typo_tolerance_via_trigram(db: Database) -> None:
    state = await make_profile(db)
    await KnowledgeService(db).add_manual(state, MOD, "Spiritmaster summons spirits to tank damage.", interaction_id=1)
    ans = await QueryService(db).answer(ask(state, "spiritmastr spirits"))
    assert ans.items, ans.route


async def test_restricted_evidence_never_leaks(db: Database) -> None:
    state = await make_profile(db)
    now = datetime.now(UTC)
    async with db.transaction() as conn:
        actor = await actors.discord_actor(conn, 2)
        src = await kstore.discord_source(conn, state.profile_id, SECRET)
        obs, rev, _ = await kstore.upsert_observation(
            conn,
            kstore.NewObservation(
                profile_id=state.profile_id,
                source_id=src,
                kind="discord_message",
                external_id="m1",
                capture_mode="explicit",
                content="Secret raid strategy: stack on the left pillar.",
                author_actor_id=actor,
                author_trust_tier=3,
                guild_id=GUILD,
                channel_id=SECRET,
                message_id=77,
                audience_channel_id=SECRET,
            ),
        )
        cid = await kstore.create_claim(
            conn,
            kstore.NewClaim(state.profile_id, "Secret raid strategy: stack on the left pillar.", "en", None),
            actor,
        )
        await kstore.add_evidence(
            conn,
            kstore.NewEvidence(
                cid,
                obs,
                rev,
                "supports",
                "stack on the left pillar",
                True,
                "human",
                3,
                "discord:user:2",
                "discord",
                now,
                audience_channel_id=SECRET,
                endorsed_by=actor,
                endorser_tier=3,
            ),
        )
        await kstore.recompute(conn, cid, state.config)
    qs = QueryService(db)
    assert not (await qs.answer(ask(state, "raid strategy pillar"))).items
    allowed = await qs.answer(ask(state, "raid strategy pillar", allowed=frozenset({SECRET})))
    assert allowed.items and allowed.items[0].sources[0].channel_id == SECRET
    ks = KnowledgeService(db)
    with pytest.raises(NotFound):
        await ks.show(state, cid, set())
    assert (await ks.show(state, cid, {SECRET}))["evidence"]


async def test_rate_limits_minute_hour_day_and_exempt(db: Database) -> None:
    def tight(c: dict[str, Any]) -> None:
        default_setup(c)
        c["rate_limits"] = {
            "default": {"per_minute": 100, "per_hour": 2, "per_day": 3, "llm_per_day": 1},
            "roles": {str(ROLE_TRUSTED): {"per_minute": 100, "per_hour": 50, "per_day": 50}},
        }

    state = await make_profile(db, tight)
    rl = RateLimiter(db, "salt")
    assert (await rl.take_query(state, MEMBER)).allowed
    assert (await rl.take_query(state, MEMBER)).allowed
    blocked = await rl.take_query(state, MEMBER)
    assert not blocked.allowed and blocked.window == "hour"
    for _ in range(5):
        assert (await rl.take_query(state, TRUSTED)).allowed  # role override
    for _ in range(5):
        assert (await rl.take_query(state, MOD)).allowed  # moderators hold ratelimit.exempt
    assert await rl.take_llm(state, MEMBER) and not await rl.take_llm(state, MEMBER)
    admin = Principal(user_id=99, is_guild_admin=True)
    for _ in range(10):
        assert (await rl.take_query(state, admin)).allowed  # superuser holds ratelimit.exempt
