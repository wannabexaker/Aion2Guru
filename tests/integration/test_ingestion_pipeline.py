from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from guru.core.permissions import Principal
from guru.db import Database
from guru.jobs.queue import Job, RetryLater
from guru.llm.client import LLMUnavailable, fake_client
from guru.llm.embeddings import HashEmbedder
from guru.services.extraction_service import ExtractionService
from guru.services.ingest_service import CapturedMessage, IngestService
from guru.services.knowledge_service import PermissionDenied
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryRequest, QueryService
from guru.services.review_service import ReviewService
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_MOD, ROLE_TRUSTED, default_setup, make_profile

pytestmark = pytest.mark.db

MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
MEMBER2 = Principal(user_id=5, role_ids=frozenset({ROLE_MEMBER}))
TRUSTED = Principal(user_id=2, role_ids=frozenset({ROLE_TRUSTED}))
MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))
MOD2 = Principal(user_id=6, role_ids=frozenset({ROLE_MOD}))

LINE = re.compile(r"^\*\[(m\d+)\] \([^)]*\) (.*)$", re.M)


def extractor(rules: dict[str, tuple[str, str, str]]) -> Any:
    """Fake LLM: for each candidate line containing a key, emit (statement, category, quote)."""

    def handle(messages: list[dict[str, str]]) -> dict[str, Any]:
        claims = []
        for sid, text in LINE.findall(messages[1]["content"]):
            for key, (statement, category, quote) in rules.items():
                if key in text:
                    claims.append(
                        {
                            "statement": statement,
                            "type": "fact",
                            "category": category,
                            "source_ids": [sid],
                            "quotes": [quote],
                        }
                    )
        return {"claims": claims}

    return handle


RULES = {
    "Fire Temple": ("The Fire Temple boss respawns every 4 hours.", "content", "respawns every 4 hours"),
    "Πυρός": ("The Fire Temple boss respawns every 4 hours.", "content", "κάθε 4 ώρες"),
    "kinah": ("Daily quests reward about 50000 kinah.", "economy", "about 50000 kinah"),
}


async def _setup(
    db: Database, handlers: dict[str, Any] | None = None, setup: Any = default_setup
) -> tuple[ProfileState, IngestService, ExtractionService]:
    state = await make_profile(db, setup)
    reg = ProfileRegistry(db)
    await reg.reload()
    llm, _ = fake_client(handlers or {"extract": extractor(RULES)})
    return reg.get(state.profile_id), IngestService(db), ExtractionService(db, reg, llm, HashEmbedder())  # type: ignore[return-value]


async def _home(
    ingest: IngestService, state: ProfileState, mid: int, who: Principal, text: str, minutes_ago: int = 0
) -> str:
    return await ingest.on_home_message(
        state,
        message_id=mid,
        channel_id=HOME,
        author=who,
        content=text,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


async def _run_window(db: Database, ex: ExtractionService, state: ProfileState) -> None:
    await ex.handle_window(Job(0, "llm.window", {"profile_id": state.profile_id, "channel_id": HOME}, 1, 5, None))


async def test_passive_candidate_extraction_review_and_answer(db: Database) -> None:
    state, ingest, ex = await _setup(db)
    assert await _home(ingest, state, 1, MEMBER2, "anyone knows when it spawns?", 3) == "context"
    assert await _home(ingest, state, 2, MEMBER, "lol") == "drop"
    assert await _home(ingest, state, 3, MEMBER, "The Fire Temple boss respawns every 4 hours btw") == "candidate"
    obs = {r["message_id"]: r for r in await db.fetch("SELECT * FROM observations")}
    assert obs[3]["processing_state"] == "pending" and obs[1]["processing_state"] == "context_only"
    assert 2 not in obs  # noise is never stored
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 'llm.window'") == 1

    await _run_window(db, ex, state)
    claim = await db.fetchrow("SELECT * FROM claims")
    assert claim["statement"] == "The Fire Temple boss respawns every 4 hours."
    assert claim["verification"] == "unverified"
    ev = await db.fetchrow("SELECT * FROM claim_evidence")
    assert ev["quote"] == "respawns every 4 hours" and ev["extractor"].startswith("llm:extract@")
    task = await db.fetchrow("SELECT * FROM review_tasks")
    assert task["kind"] == "claim_keep" and task["payload"]["quotes"][0]["message_id"] == 3
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 'discord.review_post'") == 1
    assert await db.fetchval("SELECT count(*) FROM embeddings WHERE owner_type = 'claim'") == 1

    # Moderator keeps it → verified, labeled; the answer path now returns it.
    out = await ReviewService(db).vote(state, MOD, task["id"], "keep")
    assert out.closed and out.decision == "keep"
    assert await db.fetchval("SELECT verification FROM claims") == "verified"
    assert await db.fetchval("SELECT label FROM decision_labels") == 1
    ans = await QueryService(db).answer(QueryRequest(state, MEMBER, "fire temple boss respawn", GUILD, HOME))
    assert ans.items and ans.items[0].verification == "verified"


async def test_same_fact_from_two_people_in_two_languages_corroborates(db: Database) -> None:
    state, ingest, ex = await _setup(db)
    await _home(ingest, state, 10, MEMBER, "The Fire Temple boss respawns every 4 hours")
    await _home(ingest, state, 11, MEMBER2, "Ο boss του Ναού του Πυρός βγαίνει κάθε 4 ώρες")
    await _run_window(db, ex, state)
    assert await db.fetchval("SELECT count(*) FROM claims") == 1
    assert await db.fetchval("SELECT count(*) FROM claim_evidence") == 2
    assert await db.fetchval("SELECT verification FROM claims") == "corroborated"


async def test_explicit_capture_by_trusted_is_verified_without_review(db: Database) -> None:
    state, ingest, ex = await _setup(db)
    msg = CapturedMessage(
        20, GUILD, 777, MEMBER.user_id, 2, "Daily quests reward about 50000 kinah each.", datetime.now(UTC)
    )
    with pytest.raises(PermissionDenied):
        await ingest.capture_explicit(state, MEMBER, msg, reason="reaction")
    obs_id = await ingest.capture_explicit(state, TRUSTED, msg, reason="reaction")
    await ex.handle_extract(
        Job(0, "llm.extract", {"profile_id": state.profile_id, "observation_ids": [obs_id]}, 1, 5, None)
    )
    claim = await db.fetchrow("SELECT verification, verification_basis FROM claims")
    assert claim["verification"] == "verified" and claim["verification_basis"] == "human"
    assert await db.fetchval("SELECT count(*) FROM review_tasks") == 0


async def test_hallucinated_numbers_are_rejected(db: Database) -> None:
    rules = {"Fire Temple": ("The Fire Temple boss respawns every 6 hours.", "content", "respawns every 4 hours")}
    state, ingest, ex = await _setup(db, {"extract": extractor(rules)})
    await _home(ingest, state, 30, MEMBER, "The Fire Temple boss respawns every 4 hours")
    await _run_window(db, ex, state)
    assert await db.fetchval("SELECT count(*) FROM claims") == 0
    meta = await db.fetchval("SELECT meta FROM observations WHERE message_id = 30")
    assert meta["extraction"]["rejected"] == ["V8_numbers"]
    assert await db.fetchval("SELECT processing_state FROM observations WHERE message_id = 30") == "irrelevant"


async def test_llm_down_requeues_without_losing_candidates(db: Database) -> None:
    def down(_: Any) -> dict[str, Any]:
        raise LLMUnavailable("connection refused")

    state, ingest, ex = await _setup(db, {"extract": down})
    await _home(ingest, state, 40, MEMBER, "The Fire Temple boss respawns every 4 hours")
    with pytest.raises(RetryLater):
        await _run_window(db, ex, state)
    assert await db.fetchval("SELECT processing_state FROM observations WHERE message_id = 40") == "pending"


async def test_edit_and_delete_sync(db: Database) -> None:
    state, ingest, ex = await _setup(db)
    await _home(ingest, state, 50, MEMBER, "The Fire Temple boss respawns every 4 hours")
    await _run_window(db, ex, state)
    claim_id = await db.fetchval("SELECT id FROM claims")
    # Edit → old evidence inactive, re-extraction queued, recompute queued.
    assert await ingest.on_edit(HOME, 50, "The Fire Temple boss respawns every 4 hours (confirmed)")
    assert await db.fetchval("SELECT active FROM claim_evidence") is False
    kinds = {r["kind"] for r in await db.fetch("SELECT kind FROM jobs WHERE status = 'queued'")}
    assert {"llm.extract", "knowledge.recompute"} <= kinds
    await ex.handle_extract(
        Job(
            0,
            "llm.extract",
            {
                "profile_id": state.profile_id,
                "observation_ids": [await db.fetchval("SELECT id FROM observations WHERE message_id = 50")],
            },
            1,
            5,
            None,
        )
    )
    assert await db.fetchval("SELECT count(*) FROM claim_evidence WHERE active AND claim_id = $1", claim_id) == 1
    # Delete with keep_knowledge (D-06): the knowledge stays.
    assert await ingest.on_delete(HOME, [50], purge=False) == 1
    assert await db.fetchval("SELECT status FROM observations WHERE message_id = 50") == "deleted"
    assert await db.fetchval("SELECT count(*) FROM claim_evidence WHERE active") == 1
    assert await db.fetchval("SELECT lifecycle FROM claims") == "active"


async def test_delete_with_purge_retracts_unsupported_claim(db: Database) -> None:
    state, ingest, ex = await _setup(db)
    await _home(ingest, state, 60, MEMBER, "The Fire Temple boss respawns every 4 hours")
    await _run_window(db, ex, state)
    await ingest.on_delete(HOME, [60], purge=True)
    claim_ids = await db.fetchval("SELECT payload->'claim_ids' FROM jobs WHERE kind = 'knowledge.recompute'")
    await ex.handle_recompute(
        Job(0, "knowledge.recompute", {"profile_id": state.profile_id, "claim_ids": claim_ids}, 1, 5, None)
    )
    row = await db.fetchrow("SELECT lifecycle, lifecycle_reason FROM claims")
    assert row["lifecycle"] == "retracted" and row["lifecycle_reason"] == "no_evidence"
    assert await db.fetchval("SELECT content FROM observations WHERE message_id = 60") is None


async def test_quorum_and_reject(db: Database) -> None:
    def quorum2(c: dict[str, Any]) -> None:
        default_setup(c)
        c["review"] = {"quorum": 2}

    state, ingest, ex = await _setup(db, setup=quorum2)
    await _home(ingest, state, 70, MEMBER, "Daily quests reward about 50000 kinah, nice")
    await _run_window(db, ex, state)
    task_id = await db.fetchval("SELECT id FROM review_tasks")
    rs = ReviewService(db)
    with pytest.raises(PermissionDenied):
        await rs.vote(state, MEMBER, task_id, "keep")
    first = await rs.vote(state, MOD, task_id, "reject")
    assert not first.closed
    second = await rs.vote(state, MOD2, task_id, "reject")
    assert second.closed and second.decision == "reject"
    assert await db.fetchval("SELECT lifecycle FROM claims") == "rejected"
    assert await db.fetchval("SELECT label FROM decision_labels") == 0


async def test_contradiction_opens_conflict(db: Database) -> None:
    rules = {
        "every 4 hours": ("The Fire Temple boss respawns every 4 hours.", "content", "respawns every 4 hours"),
        "every 6 hours": ("The Fire Temple boss respawns every 6 hours.", "content", "respawns every 6 hours"),
    }

    def equiv(_: Any) -> dict[str, Any]:
        return {"relation": "contradicts"}

    def low_dedupe(c: dict[str, Any]) -> None:
        default_setup(c)
        c["search"] = {"dedupe": {"tau_high": 0.999, "tau_low": 0.5}}

    state, ingest, ex = await _setup(db, {"extract": extractor(rules), "equivalence": equiv}, low_dedupe)
    await _home(ingest, state, 80, MEMBER, "Fire Temple boss respawns every 4 hours")
    await _run_window(db, ex, state)
    await db.execute("UPDATE jobs SET status = 'done'")
    await _home(ingest, state, 81, MEMBER2, "Fire Temple boss respawns every 6 hours now")
    await _run_window(db, ex, state)
    states = [r["verification"] for r in await db.fetch("SELECT verification FROM claims ORDER BY id")]
    assert states == ["disputed", "disputed"]
    assert await db.fetchval("SELECT count(*) FROM conflict_members") == 2
