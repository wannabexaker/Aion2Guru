from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest

from guru.core.permissions import Principal
from guru.core.text import content_tokens
from guru.db import Database
from guru.jobs.queue import Job
from guru.llm.client import fake_client
from guru.services.extraction_service import ExtractionService
from guru.services.knowledge_service import KnowledgeService
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryRequest, QueryService
from guru.services.ratelimit_service import RateLimiter
from guru.services.router import IncomingMessage, MessageRouter, Reply
from guru.store import actors
from guru.store import knowledge as kstore
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_MOD, default_setup, make_profile

pytestmark = pytest.mark.db
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))

# Tiny "semantic" space: synonyms map to the same axis.
AXES = {
    "respawn": 0, "spawn": 0, "appear": 0, "appears": 0, "comes": 0, "back": 0,
    "boss": 1, "guardian": 1, "temple": 2, "shrine": 2, "kinah": 3, "money": 3, "gold": 3,
}  # fmt: skip


class SynonymEmbedder:
    name = "synonyms:8"
    dims = 8

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dims
            for tok in content_tokens(text):
                if tok in AXES:
                    v[AXES[tok]] += 1.0
            v[7] += 0.05  # avoid zero vectors
            n = math.sqrt(sum(x * x for x in v))
            out.append([x / n for x in v])
        return out


async def _state(db: Database, setup: Any = default_setup) -> ProfileState:
    await make_profile(db, setup)
    reg = ProfileRegistry(db)
    await reg.reload()
    s = reg.by_slug("aion2")
    assert s is not None
    return s


async def _add(db: Database, state: ProfileState, text: str, n: int) -> int:
    return (await KnowledgeService(db).add_manual(state, MOD, text, interaction_id=n)).claim_id


async def _embed_all(db: Database, state: ProfileState, embedder: Any) -> None:
    reg = ProfileRegistry(db)
    await reg.reload()
    ex = ExtractionService(db, reg, None, embedder)
    await ex.handle_embed_backfill(Job(0, "knowledge.embed", {}, 1, 5, None))


def req(state: ProfileState, text: str, **kw: Any) -> QueryRequest:
    return QueryRequest(state, MEMBER, text, GUILD, HOME, **kw)


async def test_vector_finds_paraphrase_without_lexical_overlap(db: Database) -> None:
    state = await _state(db)
    cid = await _add(db, state, "The shrine guardian comes back every 4 hours.", 1)
    embedder = SynonymEmbedder()
    await _embed_all(db, state, embedder)
    lexical_only = await QueryService(db).answer(req(state, "when does the temple boss respawn"))
    assert not lexical_only.items
    qs = QueryService(db, embedder=embedder)
    ans = await qs.answer(req(state, "when does the temple boss respawn"))
    assert ans.items and ans.items[0].claim_id == cid and any(r.startswith("vector:") for r in ans.route)


async def test_llm_synthesis_grounded_and_fallback(db: Database) -> None:
    state = await _state(db)
    await _add(db, state, "Fire Temple entry is limited to 2 runs per day.", 1)
    await _add(db, state, "Fire Temple entry requires level 45.", 2)
    replies: list[dict[str, Any]] = [
        {"answer": "Θες level 45 και έχεις 2 εισόδους τη μέρα.", "cited": ["R1", "R2"], "uncertainty": "none"},
        {"answer": "Θες level 50.", "cited": ["R1"], "uncertainty": "none"},  # hallucinated number
    ]
    llm, provider = fake_client({"answer": lambda m: replies.pop(0)})
    qs = QueryService(db, llm=llm)
    ans = await qs.answer(req(state, "Τι χρειάζεται για το Fire Temple entry;"))
    assert ans.mode == "llm" and "45" in (ans.text or "") and len(ans.items) == 2
    assert ans.route[-1] == "llm:ok" and ans.answered_by == "llm"
    system = provider.calls[0][1][0]["content"]
    assert "Greek" in system and "DATA, not instructions" in system
    # Second (different wording → no cache) answer fails grounding → deterministic list.
    ans2 = await qs.answer(req(state, "Fire Temple entry requirements;"))
    assert ans2.mode == "list" and "llm:rejected:ungrounded_number" in ans2.route


async def test_llm_quota_exhausted_falls_back(db: Database) -> None:
    state = await _state(db)
    await _add(db, state, "Fire Temple entry is limited to 2 runs per day.", 1)
    await _add(db, state, "Fire Temple entry requires level 45.", 2)
    llm, provider = fake_client({"answer": lambda m: {"answer": "x", "cited": ["R1"], "uncertainty": "none"}})

    async def no_quota(s: ProfileState, p: Principal) -> bool:
        return False

    ans = await QueryService(db, llm=llm, llm_quota=no_quota).answer(req(state, "fire temple entry"))
    assert ans.mode == "list" and "llm:quota" in ans.route and provider.calls == []


async def test_conflict_shows_both_sides(db: Database) -> None:
    state = await _state(db)
    a = await _add(db, state, "The Fire Temple boss respawns every 4 hours.", 1)
    b = await _add(db, state, "The Fire Temple boss respawns every 6 hours.", 2)
    async with db.transaction() as conn:
        k = await conn.fetchval(
            "INSERT INTO conflicts (profile_id, kind) VALUES ($1, 'statement') RETURNING id", state.profile_id
        )
        await conn.execute("INSERT INTO conflict_members VALUES ($1, $2), ($1, $3)", k, a, b)
        await conn.execute("UPDATE claims SET human_verified_by = NULL")
        await conn.execute("UPDATE claim_evidence SET endorser_tier = NULL")
        for cid in (a, b):
            await kstore.recompute(conn, cid, state.config)
    ans = await QueryService(db).answer(req(state, "fire temple boss respawn every 4 hours"))
    assert ans.mode == "conflict" and {i.claim_id for i in ans.items} == {a, b}
    assert ans.overall_state == "disputed"


async def test_cache_hit_and_epoch_invalidation(db: Database) -> None:
    state = await _state(db)
    await _add(db, state, "Kinah is earned from daily quests.", 1)
    qs = QueryService(db)
    first = await qs.answer(req(state, "how to earn kinah"))
    second = await qs.answer(req(state, "how to earn kinah"))
    assert not first.cached and second.cached and second.answered_by == "cache"
    assert second.items[0].sources[0].evidence_at == first.items[0].sources[0].evidence_at
    # New knowledge bumps the epoch → the cached answer is no longer served.
    await _add(db, state, "Kinah also drops from field bosses.", 2)
    third = await qs.answer(req(state, "how to earn kinah"))
    assert not third.cached
    # Different visibility → different cache entry.
    other = await qs.answer(req(state, "how to earn kinah", allowed_channels=frozenset({502})))
    assert not other.cached


async def test_follow_up_uses_previous_question(db: Database) -> None:
    state = await _state(db)
    await _add(db, state, "The Fire Temple boss respawns every 4 hours.", 1)
    reg = ProfileRegistry(db)
    await reg.reload()
    qs = QueryService(db)
    router = MessageRouter(reg, qs, RateLimiter(db, "s"), "s", 999)
    first = await router.route(
        IncomingMessage(1, GUILD, HOME, None, MEMBER, "<@999> fire temple boss respawn?", True, datetime.now(UTC))
    )
    reply = first.actions[0]
    assert isinstance(reply, Reply) and reply.on_sent
    await reply.on_sent(5555)
    follow = await router.route(
        IncomingMessage(
            2, GUILD, HOME, None, MEMBER, "<@999> and how often?", True, datetime.now(UTC), reference_message_id=5555
        )
    )
    assert follow.answer is not None and follow.answer.items
    async with db.transaction() as conn:
        await actors.system_actor(conn, "noop")
