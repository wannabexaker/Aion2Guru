from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from guru.core.permissions import Principal
from guru.core.render import render_review
from guru.db import Database
from guru.jobs.queue import Job
from guru.llm.client import fake_client
from guru.llm.embeddings import HashEmbedder
from guru.services.extraction_service import ExtractionService
from guru.services.ingest_service import IngestService
from guru.services.knowledge_service import KnowledgeService
from guru.services.profiles import ProfileRegistry
from guru.services.query_service import QueryService
from guru.services.ratelimit_service import RateLimiter
from guru.services.review_service import ReviewService, submit_claim_for_review
from guru.services.router import IncomingMessage, MessageRouter
from guru.store import actors
from guru.store import knowledge as kstore
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_MOD, ROLE_TRUSTED, default_setup, make_profile

pytestmark = pytest.mark.db
BOT = 999
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
TRUSTED = Principal(user_id=2, role_ids=frozenset({ROLE_TRUSTED}))
MOD = Principal(user_id=1, role_ids=frozenset({ROLE_MOD}))


def _msg(text: str, who: Principal, mid: int = 500) -> IncomingMessage:
    return IncomingMessage(mid, GUILD, HOME, None, who, f"<@{BOT}> {text}", True, datetime.now(UTC))


async def test_teach_by_mention_and_answer_rating(db: Database) -> None:
    state = await make_profile(db)
    reg = ProfileRegistry(db)
    await reg.reload()
    router = MessageRouter(
        reg, QueryService(db), RateLimiter(db, "s"), "s", BOT, ingest=IngestService(db), review=ReviewService(db)
    )
    # Members cannot teach; trusted contributors can.
    denied = await router.route(_msg("μάθε: Ο boss του Fire Temple βγαίνει κάθε 4 ώρες", MEMBER))
    assert denied.reason == "teach:denied"
    ok = await router.route(_msg("μάθε: Ο boss του Fire Temple βγαίνει κάθε 4 ώρες", TRUSTED, 501))
    assert ok.reason == "teach:ok"
    obs = await db.fetchrow("SELECT * FROM observations WHERE message_id = 501")
    assert obs["capture_mode"] == "explicit" and obs["content"].startswith("Ο boss")
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 'llm.extract'") == 1

    # An answered question is queued for moderator rating.
    await KnowledgeService(db).add_manual(
        reg.get(state.profile_id),
        MOD,
        "Abyss points come from PvP kills.",  # type: ignore[arg-type]
        interaction_id=9,
    )
    res = await router.route(_msg("how to get abyss points?", MEMBER, 502))
    await res.actions[0].on_sent(7777)  # type: ignore[union-attr]
    task = await db.fetchrow("SELECT * FROM review_tasks WHERE kind = 'answer_rating'")
    assert task["payload"]["bot_message"]["message_id"] == 7777
    payload = render_review(dict(task), "el")
    assert [b.custom_id for b in payload.buttons] == [f"guru:rv:{task['id']}:good", f"guru:rv:{task['id']}:bad"]
    out = await ReviewService(db).vote(reg.get(state.profile_id), MOD, task["id"], "bad")  # type: ignore[arg-type]
    assert out.closed and await db.fetchval("SELECT feedback FROM query_log WHERE id = $1", task["target_id"]) == -1


async def test_learned_gate_trains_and_auto_decides(db: Database) -> None:
    def auto_on(c: dict[str, Any]) -> None:
        default_setup(c)
        c["review"] = {"claim_keep": {"enabled": True, "auto": {"enabled": True, "threshold": 0.9, "min_labels": 200}}}

    state = await make_profile(db, auto_on)
    reg = ProfileRegistry(db)
    await reg.reload()
    rng = np.random.default_rng(0)
    async with db.transaction() as conn:
        for _ in range(400):
            x = rng.normal(size=8).tolist()
            label = int(2 * x[0] - x[1] > 0)
            await conn.execute(
                """INSERT INTO decision_labels (profile_id, decision_kind, target_type, target_id, label, features,
                                                source) VALUES ($1, 'claim_keep', 'claim', 0, $2, $3, 'human')""",
                state.profile_id,
                label,
                {"x": x},
            )
    llm, _ = fake_client({"extract": lambda m: {"claims": []}})
    ex = ExtractionService(db, reg, llm, HashEmbedder())
    await ex.handle_train(Job(0, "learn.train", {}, 1, 5, None))
    gate = await db.fetchrow("SELECT * FROM learned_gates WHERE active")
    assert gate["auto_enabled"] and gate["n_labels"] == 400

    # A confidently-good candidate is auto-kept; an uncertain one goes to humans.
    async with db.transaction() as conn:
        actor = await actors.system_actor(conn, "t")
        state2 = reg.get(state.profile_id)
        assert state2 is not None
        c1 = await kstore.create_claim(
            conn, kstore.NewClaim(state.profile_id, "Fact one about bosses.", "en", None), actor
        )
        c2 = await kstore.create_claim(
            conn, kstore.NewClaim(state.profile_id, "Fact two about items.", "en", None), actor
        )
        r1 = await submit_claim_for_review(
            conn, state2.config, state.profile_id, c1, {"statement": "x"}, [5.0, -5.0, 0, 0, 0, 0, 0, 0]
        )
        r2 = await submit_claim_for_review(
            conn, state2.config, state.profile_id, c2, {"statement": "y"}, [0.0, 0.0, 0, 0, 0, 0, 0, 0]
        )
    assert r1 == "auto_keep" and r2 == "review"
    assert await db.fetchval("SELECT verification FROM claims WHERE id = $1", c1) == "verified"
    assert await db.fetchval("SELECT count(*) FROM decision_labels WHERE source = 'auto'") == 1
