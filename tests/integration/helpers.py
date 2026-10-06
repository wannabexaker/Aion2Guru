from __future__ import annotations

from collections.abc import Callable
from typing import Any

from guru.core.config import load_yaml
from guru.db import Database
from guru.profiles import load_template
from guru.services.config_service import ConfigService
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.store import actors

GUILD = 111
HOME = 500
MOD = 501
SECRET = 502  # restricted channel
ROLE_MEMBER = 42
ROLE_TRUSTED = 43
ROLE_MOD = 44


def default_setup(c: dict[str, Any]) -> None:
    c["channels"] = [
        {"channel_id": HOME, "role": "home"},
        {"channel_id": MOD, "role": "mod_review"},
        {"channel_id": SECRET, "role": "watch", "audience": "restricted"},
    ]
    c["role_groups"] = {
        "ai_users": [ROLE_MEMBER],
        "contributors": [ROLE_TRUSTED],
        "moderators": [ROLE_MOD],
    }
    c["trust"]["roles"] = {str(ROLE_TRUSTED): 3, str(ROLE_MOD): 3}


async def make_profile(db: Database, setup: Callable[[dict[str, Any]], None] = default_setup) -> ProfileState:
    async with db.transaction() as conn:
        actor = await actors.system_actor(conn, "test")
    cfg = load_yaml(load_template("aion2"))
    cfg = cfg.model_copy(update={"profile": cfg.profile.model_copy(update={"guild_id": GUILD})})
    svc = ConfigService(db)
    await svc.apply(cfg, actor)
    await svc.patch("aion2", setup, actor, "test setup")
    reg = ProfileRegistry(db)
    await reg.reload()
    state = reg.by_slug("aion2")
    assert state is not None
    return state
