from __future__ import annotations

from typing import Any

import pytest

from guru.core.permissions import Principal
from guru.db import Database
from guru.jobs.queue import Job
from guru.llm.client import fake_client
from guru.services.faq_service import FaqService
from guru.services.knowledge_service import KnowledgeService, PermissionDenied
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryRequest, QueryService
from guru.services.review_service import ReviewService
from guru.store import knowledge as kstore
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_MOD, default_setup, make_profile

pytestmark = pytest.mark.db
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))
MOD2 = Principal(user_id=6, role_ids=frozenset({ROLE_MOD}))
FAQ_CHANNEL = 600


def with_faq(c: dict[str, Any]) -> None:
    default_setup(c)
    c["channels"].append({"channel_id": FAQ_CHANNEL, "role": "faq_publish"})


async def _setup(db: Database, llm_reply: Any = None, setup: Any = with_faq) -> tuple[ProfileState, FaqService]:
    await make_profile(db, setup)
    reg = ProfileRegistry(db)
    await reg.reload()
    state = reg.by_slug("aion2")
    assert state is not None
    llm = None
    if llm_reply is not None:
        llm, _ = fake_client({"faq_draft": llm_reply})
    return state, FaqService(db, reg, llm)


async def _verified_claim(db: Database, state: ProfileState, text: str, n: int = 1) -> int:
    return (await KnowledgeService(db).add_manual(state, MOD, text, category_key="content", interaction_id=n)).claim_id


async def test_candidate_draft_approve_publish_and_answer(db: Database) -> None:
    def draft(_: Any) -> dict[str, Any]:
        return {"question": "How often does the Fire Temple boss respawn?", "answer": "Every 4 hours.", "cited": ["R1"]}

    state, faq = await _setup(db, draft)
    cid = await _verified_claim(db, state, "The Fire Temple boss respawns every 4 hours.")
    await faq.handle_candidates(Job(0, "faq.candidates", {}, 1, 5, None))
    entry = await db.fetchrow("SELECT * FROM faq_entries")
    assert entry["status"] == "pending_approval" and entry["question"].startswith("How often")
    assert await db.fetchval("SELECT claim_id FROM faq_claims") == cid
    task = await db.fetchrow("SELECT * FROM review_tasks WHERE kind = 'faq_approval'")
    assert task is not None
    # Candidates are not duplicated on the next run.
    await faq.handle_candidates(Job(0, "faq.candidates", {}, 1, 5, None))
    assert await db.fetchval("SELECT count(*) FROM faq_entries") == 1

    rs = ReviewService(db, faq=faq)
    with pytest.raises(PermissionDenied):
        await rs.vote(state, MEMBER, task["id"], "approve")
    out = await rs.vote(
        state, MOD, task["id"], "edit", faq_edit=("Fire Temple boss respawn time?", "The boss respawns every 4 hours.")
    )
    assert out.closed and out.decision == "edit"
    entry = await db.fetchrow("SELECT * FROM faq_entries")
    assert (
        entry["status"] == "published" and entry["rev"] == 2 and entry["question"] == "Fire Temple boss respawn time?"
    )
    pub = await db.fetchrow("SELECT * FROM faq_publications")
    assert pub["desired_state"] == "published" and pub["desired_rev"] == 2 and pub["channel_id"] == FAQ_CHANNEL
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 'discord.faq_sync'") == 1

    # FAQ-first answering: verbatim, no retrieval ranking, no LLM.
    qs = QueryService(db, faq=faq)
    ans = await qs.answer(QueryRequest(state, MEMBER, "fire temple boss respawn time?", GUILD, HOME))
    assert ans.mode == "faq" and ans.faq and ans.faq["id"] == entry["id"] and ans.answered_by == "faq"
    faq_only = await qs.answer(QueryRequest(state, MEMBER, "faq: how do I enchant gear", GUILD, HOME))
    assert faq_only.mode == "no_answer"


async def test_template_draft_without_llm(db: Database) -> None:
    state, faq = await _setup(db)
    await _verified_claim(db, state, "Dungeon entries reset daily at 09:00 server time.")
    await faq.handle_candidates(Job(0, "faq.candidates", {}, 1, 5, None))
    task = await db.fetchrow("SELECT payload FROM review_tasks WHERE kind = 'faq_approval'")
    assert task["payload"]["needs_edit"] is True
    assert await db.fetchval("SELECT answer FROM faq_entries") == "Dungeon entries reset daily at 09:00 server time."


async def test_ungrounded_llm_draft_falls_back_to_template(db: Database) -> None:
    def bad(_: Any) -> dict[str, Any]:
        return {"question": "When does it reset?", "answer": "At 10:00.", "cited": ["R1"]}

    state, faq = await _setup(db, bad)
    await _verified_claim(db, state, "Dungeon entries reset daily at 09:00 server time.")
    await faq.handle_candidates(Job(0, "faq.candidates", {}, 1, 5, None))
    assert await db.fetchval("SELECT answer FROM faq_entries") == "Dungeon entries reset daily at 09:00 server time."


async def test_no_faq_channel_means_no_candidates(db: Database) -> None:
    state, faq = await _setup(db, setup=default_setup)
    await _verified_claim(db, state, "The Fire Temple boss respawns every 4 hours.")
    await faq.handle_candidates(Job(0, "faq.candidates", {}, 1, 5, None))
    assert await db.fetchval("SELECT count(*) FROM faq_entries") == 0


async def _published(db: Database, state: ProfileState, faq: FaqService, text: str) -> tuple[int, int]:
    cid = await _verified_claim(db, state, text)
    faq_id = await faq.create(state, [cid], origin="admin")
    assert faq_id is not None
    task_id = await db.fetchval("SELECT id FROM review_tasks WHERE target_id = $1 AND kind = 'faq_approval'", faq_id)
    await ReviewService(db, faq=faq).vote(state, MOD, task_id, "approve")
    return cid, faq_id


async def test_retracted_knowledge_deprecates_faq(db: Database) -> None:
    state, faq = await _setup(db)
    cid, faq_id = await _published(db, state, faq, "The Fire Temple boss respawns every 4 hours.")
    await KnowledgeService(db).set_lifecycle(state, MOD, cid, "retracted", "wrong")
    await faq.handle_check(Job(0, "faq.check", {}, 1, 5, None))
    row = await db.fetchrow("SELECT status, banner FROM faq_entries WHERE id = $1", faq_id)
    assert row["status"] == "needs_review" and row["banner"] == "review"
    assert await db.fetchval("SELECT desired_state FROM faq_publications WHERE faq_id = $1", faq_id) == "deprecated"
    assert (
        await db.fetchval(
            "SELECT count(*) FROM review_tasks WHERE kind = 'faq_approval' AND status = 'open' AND target_id = $1",
            faq_id,
        )
        == 1
    )


async def test_disputed_knowledge_adds_banner_and_clears_it(db: Database) -> None:
    state, faq = await _setup(db)
    cid, faq_id = await _published(db, state, faq, "The Fire Temple boss respawns every 4 hours.")
    other = await _verified_claim(db, state, "The Fire Temple boss respawns every 6 hours.", 2)
    async with db.transaction() as conn:
        k = await conn.fetchval(
            "INSERT INTO conflicts (profile_id, kind) VALUES ($1, 'statement') RETURNING id", state.profile_id
        )
        await conn.execute("INSERT INTO conflict_members VALUES ($1, $2), ($1, $3)", k, cid, other)
        await conn.execute("UPDATE claims SET human_verified_by = NULL")
        await conn.execute("UPDATE claim_evidence SET endorser_tier = NULL")
        for c in (cid, other):
            await kstore.recompute(conn, c, state.config)
    await faq.handle_check(Job(0, "faq.check", {}, 1, 5, None))
    assert await db.fetchval("SELECT banner FROM faq_entries WHERE id = $1", faq_id) == "disputed"
    await db.execute("UPDATE conflicts SET status = 'resolved'")
    async with db.transaction() as conn:
        await kstore.recompute(conn, cid, state.config)
    await faq.handle_check(Job(0, "faq.check", {}, 1, 5, None))
    assert await db.fetchval("SELECT banner FROM faq_entries WHERE id = $1", faq_id) is None


async def test_four_eyes(db: Database) -> None:
    def four_eyes(c: dict[str, Any]) -> None:
        with_faq(c)
        c["faq"] = {"require_four_eyes": True}

    state, faq = await _setup(db, setup=four_eyes)
    cid = await _verified_claim(db, state, "The Fire Temple boss respawns every 4 hours.")
    faq_id = await faq.create_from_record(state, MOD, cid)
    task_id = await db.fetchval("SELECT id FROM review_tasks WHERE target_id = $1", faq_id)
    rs = ReviewService(db, faq=faq)
    with pytest.raises(PermissionDenied):
        await rs.vote(state, MOD, task_id, "approve")  # creator cannot approve
    assert (await rs.vote(state, MOD2, task_id, "approve")).closed
