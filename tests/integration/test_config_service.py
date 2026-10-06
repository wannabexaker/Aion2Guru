from __future__ import annotations

from typing import Any

import pytest

from guru.core.config import ConfigError, load_yaml
from guru.core.permissions import Principal
from guru.db import Database
from guru.profiles import load_template
from guru.services.config_service import ConfigService
from guru.services.profiles import ProfileRegistry
from guru.store import actors, audit

pytestmark = pytest.mark.db

GUILD = 111


async def _actor(db: Database) -> int:
    async with db.transaction() as conn:
        return await actors.system_actor(conn, "test")


def _cfg(**profile: Any) -> Any:
    cfg = load_yaml(load_template("aion2"))
    return cfg.model_copy(update={"profile": cfg.profile.model_copy(update={"guild_id": GUILD, **profile})})


async def test_apply_creates_version_materializes_and_audits(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    res = await svc.apply(_cfg(), actor, comment="init", guild_name="Test Guild")
    assert res.version == 1 and res.diff
    assert await db.fetchval("SELECT count(*) FROM categories WHERE profile_id = $1 AND enabled", res.profile_id) >= 10
    assert await db.fetchval("SELECT parent_id IS NOT NULL FROM categories WHERE key = 'skills'")
    assert await db.fetchval("SELECT count(*) FROM aliases WHERE origin = 'config' AND target_type = 'category'") > 20
    assert await db.fetchval("SELECT count(*) FROM sources WHERE origin = 'system'") == 2
    assert await db.fetchval("SELECT default_profile_id FROM guilds WHERE guild_id = $1", GUILD) == res.profile_id
    async with db.connection() as conn:
        rows = await audit.query(conn, action_prefix="config.")
        assert [r["action"] for r in rows] == ["config.apply"]
        assert await audit.verify_chain(conn) is None


async def test_apply_identical_is_noop(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    await svc.apply(_cfg(), actor)
    again = await svc.apply(_cfg(), actor)
    assert again.version is None and again.diff == []
    assert await db.fetchval("SELECT count(*) FROM profile_config_versions") == 1


async def test_category_ids_stable_and_removed_ones_disabled(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    await svc.apply(_cfg(), actor)
    before = await db.fetchval("SELECT id FROM categories WHERE key = 'items'")

    def drop_lore(c: dict[str, Any]) -> None:
        c["categories"] = [x for x in c["categories"] if x["key"] != "lore"]

    res = await svc.patch("aion2", drop_lore, actor, "drop lore")
    assert res.version == 2 and any("categories" in d for d in res.diff)
    assert await db.fetchval("SELECT id FROM categories WHERE key = 'items'") == before
    assert await db.fetchval("SELECT enabled FROM categories WHERE key = 'lore'") is False


async def test_patch_validation_failure_changes_nothing(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    await svc.apply(_cfg(), actor)

    def break_it(c: dict[str, Any]) -> None:
        c["channels"] = [{"channel_id": 1, "role": "home", "default_category": "missing"}]

    with pytest.raises(ConfigError):
        await svc.patch("aion2", break_it, actor, "bad")
    assert await db.fetchval("SELECT count(*) FROM profile_config_versions") == 1


async def test_rollback_and_export(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    await svc.apply(_cfg(), actor)

    def set_notice(c: dict[str, Any]) -> None:
        c["access"]["unauthorized_action"] = "notice"

    await svc.patch("aion2", set_notice, actor, "notice")
    res = await svc.rollback("aion2", 1, actor)
    assert res.version == 3
    _, version, cfg = await svc.load_active("aion2")
    assert version == 3 and cfg.access.unauthorized_action == "delete"
    exported = load_yaml(await svc.export("aion2"))
    assert exported.config_hash() == cfg.config_hash()


async def test_guild_mismatch_rejected(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)
    await svc.apply(_cfg(), actor)
    with pytest.raises(ConfigError, match="another guild"):
        await svc.apply(_cfg(guild_id=999), actor)


async def test_registry_resolves_channels_permissions_and_aliases(db: Database) -> None:
    svc = ConfigService(db)
    actor = await _actor(db)

    def setup(c: dict[str, Any]) -> None:
        c["channels"] = [{"channel_id": 500, "role": "home"}, {"channel_id": 501, "role": "mod_review"}]
        c["permissions"] = [{"capability": "kb.query", "roles": [42]}]
        c["trust"]["roles"] = {"43": 3}

    await svc.apply(_cfg(), actor)
    await svc.patch("aion2", setup, actor, "setup")
    reg = ProfileRegistry(db)
    await reg.reload()
    state = reg.answering_profile(GUILD, 500)
    assert state is not None and state.home_channel_ids == {500} and state.mod_review_channel_id == 501
    assert reg.answering_profile(GUILD, 12345) is state  # mention anywhere → guild profile
    member = Principal(user_id=7, role_ids=frozenset({42}))
    stranger = Principal(user_id=8)
    assert state.resolver.has(member, "kb.query") and not state.resolver.has(stranger, "kb.query")
    assert state.resolver.trust_tier(Principal(user_id=9, role_ids=frozenset({43}))) == 3
    hits = state.matcher.match("pou einai to dungeon me ton boss")
    assert {h.target.target_key for h in hits} == {"content"}
