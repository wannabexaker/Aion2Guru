from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from guru.core.permissions import Principal
from guru.db import Database
from guru.jobs.queue import Job
from guru.llm.client import fake_client
from guru.llm.embeddings import HashEmbedder
from guru.services.extraction_service import ExtractionService
from guru.services.knowledge_service import PermissionDenied
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryRequest, QueryService
from guru.services.web_service import WebService
from guru.settings import WebSettings
from guru.web.fetcher import SafeFetcher
from guru.web.search import SearchHit
from tests.integration.helpers import GUILD, HOME, ROLE_MEMBER, ROLE_TRUSTED, default_setup, make_profile

pytestmark = pytest.mark.db
MEMBER = Principal(user_id=3, role_ids=frozenset({ROLE_MEMBER}))
TRUSTED = Principal(user_id=2, role_ids=frozenset({ROLE_TRUSTED}))

PAGE = """<html><head><title>AION 2 Fire Temple guide</title>
<meta property="article:published_time" content="2026-09-01T10:00:00Z"></head><body><article>
<h1>Fire Temple dungeon guide</h1>
<p>The Fire Temple dungeon boss respawns every 4 hours. Players need level 45 to enter the dungeon.</p>
<p>The first room has three guardians and the boss uses a fire shield that must be broken first.</p>
<p>Each run rewards players with tokens that can be exchanged for gear at the vendor in town.</p>
<p>Groups should bring a healer because the second phase deals heavy area damage to everyone nearby.</p>
<p>After the shield breaks, the boss becomes vulnerable for twenty seconds, so save your skills for it.</p>
<p>Loot is distributed by need or greed, and rare items can be traded only within the party for a while.</p>
</article></body></html>"""

OFFTOPIC = """<html><head><title>Cooking</title></head><body><article>
<p>This recipe for bread needs flour, water and salt. Bake it for forty minutes at a high temperature.</p>
<p>Let it rest before cutting. Serve with butter and enjoy it with your family and friends at dinner.</p>
</article></body></html>"""

LINE = re.compile(r"^\*\[(c\d+)\] \([^)]*\) (.*)$", re.M | re.S)


def _site(pages: dict[str, Any]) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        page = pages.get(f"{req.headers['host']}{req.url.path}")
        if page is None:
            return httpx.Response(404)
        if isinstance(page, httpx.Response):
            return page
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _extract(messages: list[dict[str, str]]) -> dict[str, Any]:
    user = messages[1]["content"]
    claims = []
    for sid in re.findall(r"\*\[(c\d+)\]", user):
        claims.append(
            {
                "statement": "The Fire Temple dungeon boss respawns every 4 hours.",
                "type": "fact",
                "category": "content",
                "source_ids": [sid],
                "quotes": ["boss respawns every 4 hours"],
            }
        )
        break
    return {"claims": claims}


async def _service(
    db: Database,
    pages: dict[str, Any],
    search: Any = None,
    setup: Any = default_setup,
) -> tuple[ProfileState, WebService, ExtractionService]:
    await make_profile(db, setup)
    reg = ProfileRegistry(db)
    await reg.reload()
    state = reg.by_slug("aion2")
    assert state is not None

    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    fetcher = SafeFetcher(WebSettings(min_host_interval_s=0), _site(pages), resolver)
    llm, _ = fake_client({"extract": _extract})
    ex = ExtractionService(db, reg, llm, HashEmbedder())
    return state, WebService(db, reg, fetcher, ex, search), ex


async def _drain(db: Database, web: WebService, kind: str) -> None:
    for row in await db.fetch("SELECT id, payload FROM jobs WHERE kind = $1 AND status = 'queued'", kind):
        handler = {"web.fetch_url": web.handle_fetch_url, "llm.web_extract": web.handle_web_extract}[kind]
        await handler(Job(row["id"], kind, row["payload"], 1, 5, None))
        await db.execute("UPDATE jobs SET status = 'done' WHERE id = $1", row["id"])


async def test_ingest_url_to_claim_with_web_provenance(db: Database) -> None:
    state, web, _ = await _service(db, {"guides.example/fire": PAGE})
    with pytest.raises(PermissionDenied):
        await web.ingest_url(state, MEMBER, "https://guides.example/fire")
    await web.ingest_url(state, TRUSTED, "https://guides.example/fire")
    await _drain(db, web, "web.fetch_url")
    obs = await db.fetchrow("SELECT * FROM observations WHERE kind = 'web_document'")
    assert "Fire Temple" in obs["title"] and obs["date_confidence"] == "metadata"
    assert obs["published_at"].date().isoformat() == "2026-09-01" and obs["simhash"] is not None
    assert await db.fetchval("SELECT count(*) FROM chunks WHERE active") >= 1
    await _drain(db, web, "llm.web_extract")
    claim = await db.fetchrow("SELECT * FROM claims")
    ev = await db.fetchrow("SELECT * FROM claim_evidence")
    assert ev["origin"] == "web" and ev["chunk_id"] is not None and ev["trust_tier"] == 1
    assert ev["independence_group"] == "site:guides.example"
    assert claim["verification"] == "unverified" and claim["origins"] == ["web"]
    # web scope finds it, internal scope does not
    qs = QueryService(db)
    assert (await qs.answer(QueryRequest(state, MEMBER, "web: fire temple boss respawn", GUILD, HOME))).items
    assert not (await qs.answer(QueryRequest(state, MEMBER, "internal: fire temple boss respawn", GUILD, HOME))).items


async def test_offtopic_page_is_never_sent_to_the_llm(db: Database) -> None:
    state, web, _ = await _service(db, {"food.example/bread": OFFTOPIC})
    await web.ingest_url(state, TRUSTED, "https://food.example/bread")
    await _drain(db, web, "web.fetch_url")
    assert await db.fetchval("SELECT processing_state FROM observations") == "irrelevant"
    assert await db.fetchval("SELECT count(*) FROM jobs WHERE kind = 'llm.web_extract'") == 0


async def test_syndicated_copy_shares_independence_group(db: Database) -> None:
    copy = PAGE.replace("tokens", "coins")
    state, web, _ = await _service(db, {"guides.example/fire": PAGE, "mirror.example/fire": copy})
    await web.ingest_url(state, TRUSTED, "https://guides.example/fire")
    await _drain(db, web, "web.fetch_url")
    await web.ingest_url(state, TRUSTED, "https://mirror.example/fire")
    await _drain(db, web, "web.fetch_url")
    await _drain(db, web, "llm.web_extract")
    groups = {r["independence_group"] for r in await db.fetch("SELECT independence_group FROM claim_evidence")}
    assert groups == {"site:guides.example"}  # a copy is not independent corroboration
    assert await db.fetchval("SELECT verification FROM claims") == "unverified"


async def test_gone_page_deactivates_evidence(db: Database) -> None:
    pages: dict[str, Any] = {"guides.example/fire": PAGE}
    state, web, _ = await _service(db, pages)
    await web.ingest_url(state, TRUSTED, "https://guides.example/fire")
    await _drain(db, web, "web.fetch_url")
    await _drain(db, web, "llm.web_extract")
    src = dict(await db.fetchrow("SELECT * FROM sources WHERE key = 'site:guides.example'"))
    pages["guides.example/fire"] = httpx.Response(410)
    for _ in range(3):
        await web.fetch_document(state, src, "https://guides.example/fire", mode="crawl")
    assert await db.fetchval("SELECT status FROM observations WHERE kind = 'web_document'") == "unreachable"
    assert await db.fetchval("SELECT active FROM claim_evidence") is False


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, language: str = "en", limit: int = 10) -> list[SearchHit]:
        self.queries.append(query)
        return [
            SearchHit("https://guides.example/fire", "Fire", ""),
            SearchHit("http://127.0.0.1/admin", "evil", ""),
            SearchHit("https://blocked.example/x", "spam", ""),
        ]


async def test_discovery_budget_and_domain_rules(db: Database) -> None:
    def setup(c: dict[str, Any]) -> None:
        default_setup(c)
        c["domain_deny"] = ["blocked.example"]
        c["web"]["discovery"]["max_queries_per_day"] = 2

    search = FakeSearch()
    _, web, _ = await _service(db, {"guides.example/fire": PAGE}, search, setup)
    await web.handle_discover(Job(0, "web.discover", {}, 1, 5, None))
    await web.handle_discover(Job(0, "web.discover", {}, 1, 5, None))
    assert len(search.queries) == 2  # daily budget across runs
    urls = [r["payload"]["url"] for r in await db.fetch("SELECT payload FROM jobs WHERE kind = 'web.fetch_url'")]
    assert urls == ["https://guides.example/fire"]  # private and denied hosts dropped
    assert await db.fetchval("SELECT trust_tier FROM sources WHERE key = 'site:guides.example'") == 1


async def test_reputation_promotes_reliable_discovered_source(db: Database) -> None:
    state, web, _ = await _service(db, {"guides.example/fire": PAGE})
    await web.ingest_url(state, TRUSTED, "https://guides.example/fire")
    await _drain(db, web, "web.fetch_url")
    await _drain(db, web, "llm.web_extract")
    # Pretend the team verified 10 claims backed by this source.
    src_id = await db.fetchval("SELECT id FROM sources WHERE key = 'site:guides.example'")
    obs_id = await db.fetchval("SELECT id FROM observations WHERE source_id = $1", src_id)
    actor = await db.fetchval("SELECT id FROM actors LIMIT 1")
    for i in range(10):
        cid = await db.fetchval(
            """INSERT INTO claims (profile_id, kind, statement, search_text, lang, applicability_hash, verification,
                                   verification_basis, human_verified_by)
               VALUES ($1, 'statement', $2, $2, 'en', '\\x00', 'verified', 'human', $3) RETURNING id""",
            state.profile_id,
            f"fact {i}",
            actor,
        )
        await db.execute(
            """INSERT INTO claim_evidence (claim_id, observation_id, observation_rev, stance, quote_verified, extractor,
                                           trust_tier, independence_group, origin, evidence_at)
               VALUES ($1, $2, 1, 'supports', true, 'llm:x', 1, 'site:guides.example', 'web', now())""",
            cid,
            obs_id,
        )
    await web.handle_reputation(Job(0, "web.reputation", {}, 1, 5, None))
    row = await db.fetchrow("SELECT trust_tier, reputation FROM sources WHERE id = $1", src_id)
    assert row["trust_tier"] == 3 and row["reputation"]["agree"] == 10
    assert await db.fetchval("SELECT count(*) FROM audit_log WHERE action = 'source.trust_auto'") == 1
