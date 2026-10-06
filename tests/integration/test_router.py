from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from guru.core.permissions import Principal
from guru.db import Database
from guru.services.knowledge_service import KnowledgeService
from guru.services.profiles import ProfileRegistry
from guru.services.query_service import QueryService
from guru.services.ratelimit_service import RateLimiter
from guru.services.router import Delete, IncomingMessage, MessageRouter, Notice, Reply
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_MOD, default_setup, make_profile

pytestmark = pytest.mark.db

BOT = 999
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
STRANGER = Principal(user_id=4)
MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))


def msg(content: str, who: Principal = MEMBER, channel: int = HOME, mention: bool = True) -> IncomingMessage:
    return IncomingMessage(
        message_id=1000,
        guild_id=GUILD,
        channel_id=channel,
        parent_channel_id=None,
        author=who,
        content=(f"<@{BOT}> " if mention else "") + content,
        mentions_bot=mention,
        created_at=datetime.now(UTC),
    )


async def _router(db: Database, setup: Any = default_setup) -> tuple[MessageRouter, list[Any]]:
    await make_profile(db, setup)
    reg = ProfileRegistry(db)
    await reg.reload()
    home_seen: list[Any] = []

    async def on_home(state, m):  # type: ignore[no-untyped-def]
        home_seen.append(m)

    return MessageRouter(reg, QueryService(db), RateLimiter(db, "salt"), "salt", BOT, on_home), home_seen


async def test_stranger_mention_is_deleted_without_db_work(db: Database) -> None:
    router, _ = await _router(db)
    res = await router.route(msg("what is the best class?", who=STRANGER, channel=12345))
    assert res.reason == "denied:delete" and res.actions == [Delete(12345, 1000)]
    assert await db.fetchval("SELECT count(*) FROM query_log") == 0
    assert await db.fetchval("SELECT count(*) FROM usage_counters") == 0


async def test_stranger_chatter_in_home_is_deleted_elsewhere_ignored(db: Database) -> None:
    router, home_seen = await _router(db)
    assert (await router.route(msg("hello", who=STRANGER, mention=False))).reason == "denied:delete"
    assert (await router.route(msg("hello", who=STRANGER, channel=777, mention=False))).reason == "not_addressed"
    assert (await router.route(msg("Gladiator is tanky", mention=False))).reason == "home_ingest"
    assert len(home_seen) == 1


async def test_notice_mode(db: Database) -> None:
    def setup(c: dict[str, Any]) -> None:
        default_setup(c)
        c["access"]["unauthorized_action"] = "notice"

    router, _ = await _router(db, setup)
    res = await router.route(msg("hi", who=STRANGER))
    assert isinstance(res.actions[0], Delete) and isinstance(res.actions[1], Notice)
    assert res.actions[1].payload.delete_after == 8


async def test_member_gets_answer_and_query_logged(db: Database) -> None:
    router, _ = await _router(db)
    state = router.registry.by_slug("aion2")
    assert state is not None
    await KnowledgeService(db).add_manual(state, MOD, "Abyss points are earned from PvP kills.", interaction_id=1)
    res = await router.route(msg("how do I earn abyss points?"))
    assert res.reason == "answered:extractive"
    reply = res.actions[0]
    assert isinstance(reply, Reply) and reply.payload.embed and "Abyss points" in reply.payload.embed.description
    assert reply.on_sent is not None
    await reply.on_sent(4242)
    row = await db.fetchrow("SELECT answered_by, bot_message_id, claim_ids FROM query_log")
    assert row["answered_by"] == "extractive" and row["bot_message_id"] == 4242 and row["claim_ids"]


async def test_member_rate_limited(db: Database) -> None:
    def setup(c: dict[str, Any]) -> None:
        default_setup(c)
        c["rate_limits"] = {"default": {"per_minute": 2, "per_hour": 100, "per_day": 100}}

    router, _ = await _router(db, setup)
    assert (await router.route(msg("q1"))).reason.startswith("answered")
    assert (await router.route(msg("q2"))).reason.startswith("answered")
    limited = await router.route(msg("q3"))
    assert limited.reason == "limited:minute"
    payload = limited.actions[0].payload  # type: ignore[union-attr]
    assert payload.delete_after and payload.content


async def test_dm_and_bots_ignored(db: Database) -> None:
    router, _ = await _router(db)
    dm = IncomingMessage(1, None, 1, None, MEMBER, f"<@{BOT}> hi", True, datetime.now(UTC))
    assert (await router.route(dm)).reason == "dm_ignored"
    bot = Principal(user_id=77, is_bot=True)
    assert (await router.route(msg("hi", who=bot))).reason == "bot_ignored"
