"""Runtime view of profiles: config, ids, alias matcher, permission resolver.

Loaded from the DB and refreshed when a config apply sends NOTIFY guru_config.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import asyncpg

from guru.core.aliases import AliasMatcher, AliasTarget
from guru.core.config import ProfileConfig, parse_config
from guru.core.permissions import Grant, PermissionResolver, TrustRules
from guru.db import Database
from guru.logging import get_logger
from guru.services.config_service import CONFIG_NOTIFY_CHANNEL

log = get_logger(__name__)


@dataclass
class ProfileState:
    profile_id: int
    slug: str
    guild_id: int
    version: int
    knowledge_epoch: int
    config: ProfileConfig
    category_ids: dict[str, int]
    category_keys: dict[int, str]
    source_ids: dict[str, int]
    matcher: AliasMatcher
    resolver: PermissionResolver
    channel_roles: dict[int, set[str]] = field(default_factory=dict)

    def roles_of(self, channel_id: int) -> set[str]:
        return self.channel_roles.get(channel_id, set())

    @property
    def home_channel_ids(self) -> set[int]:
        return {c for c, roles in self.channel_roles.items() if "home" in roles}

    @property
    def mod_review_channel_id(self) -> int | None:
        for c, roles in self.channel_roles.items():
            if "mod_review" in roles:
                return c
        return None


async def load_profile(conn: asyncpg.Connection, profile_id: int, owner_ids: frozenset[int]) -> ProfileState | None:
    row = await conn.fetchrow(
        """SELECT p.id, p.slug, p.guild_id, p.knowledge_epoch, v.version, v.config
             FROM profiles p JOIN profile_config_versions v ON v.id = p.active_config_id
            WHERE p.id = $1 AND p.status = 'active'""",
        profile_id,
    )
    if row is None:
        return None
    cfg = parse_config(row["config"])
    cats = await conn.fetch("SELECT id, key FROM categories WHERE profile_id = $1 AND enabled", profile_id)
    sources = await conn.fetch("SELECT id, key FROM sources WHERE profile_id = $1", profile_id)
    aliases = await conn.fetch(
        "SELECT target_type, target_key, alias, weight FROM aliases WHERE profile_id = $1", profile_id
    )
    grants = await conn.fetch(
        "SELECT capability, subject_type, subject_id FROM permission_grants WHERE profile_id = $1", profile_id
    )
    matcher = AliasMatcher()
    for a in aliases:
        matcher.add(
            a["alias"],
            AliasTarget(a["target_type"], a["target_key"], a["weight"]),
            with_greeklish=cfg.profile.languages.greeklish,
        )
    resolver = PermissionResolver(
        [Grant(g["capability"], g["subject_type"], g["subject_id"]) for g in grants],
        TrustRules(
            default_member_tier=cfg.trust.default_member_tier,
            new_account_days=cfg.trust.new_account_days,
            roles=dict(cfg.trust.roles),
            users=dict(cfg.trust.users),
            webhooks=dict(cfg.trust.webhooks),
        ),
        owner_ids=owner_ids,
        bootstrap_admins=cfg.access.bootstrap_admins,
    )
    channel_roles: dict[int, set[str]] = {}
    for ch in cfg.channels:
        channel_roles.setdefault(ch.channel_id, set()).add(ch.role)
    return ProfileState(
        profile_id=row["id"],
        slug=row["slug"],
        guild_id=row["guild_id"],
        version=row["version"],
        knowledge_epoch=row["knowledge_epoch"],
        config=cfg,
        category_ids={r["key"]: r["id"] for r in cats},
        category_keys={r["id"]: r["key"] for r in cats},
        source_ids={r["key"]: r["id"] for r in sources},
        matcher=matcher,
        resolver=resolver,
        channel_roles=channel_roles,
    )


class ProfileRegistry:
    def __init__(self, db: Database, owner_ids: frozenset[int] = frozenset()) -> None:
        self.db = db
        self.owner_ids = owner_ids
        self._by_id: dict[int, ProfileState] = {}
        self._lock = asyncio.Lock()
        self._listener: asyncpg.Connection | None = None

    async def reload(self) -> None:
        async with self._lock, self.db.connection() as conn:
            ids = [r["id"] for r in await conn.fetch("SELECT id FROM profiles WHERE status = 'active'")]
            fresh: dict[int, ProfileState] = {}
            for pid in ids:
                state = await load_profile(conn, pid, self.owner_ids)
                if state is not None:
                    fresh[pid] = state
            self._by_id = fresh
        log.info("profiles.loaded", profiles=[s.slug for s in self._by_id.values()])

    async def listen(self, dsn: str) -> None:
        """Reload on config changes (NOTIFY from ConfigService.apply)."""

        def _on_notify(*_: object) -> None:
            asyncio.get_running_loop().create_task(self.reload())

        self._listener = await asyncpg.connect(dsn)
        await self._listener.add_listener(CONFIG_NOTIFY_CHANNEL, _on_notify)

    async def close(self) -> None:
        if self._listener is not None:
            await self._listener.close()

    def all(self) -> list[ProfileState]:
        return list(self._by_id.values())

    def get(self, profile_id: int) -> ProfileState | None:
        return self._by_id.get(profile_id)

    def by_slug(self, slug: str) -> ProfileState | None:
        return next((p for p in self._by_id.values() if p.slug == slug), None)

    def for_guild(self, guild_id: int) -> list[ProfileState]:
        return [p for p in self._by_id.values() if p.guild_id == guild_id]

    def answering_profile(
        self, guild_id: int, channel_id: int, parent_channel_id: int | None = None
    ) -> ProfileState | None:
        """Profile that answers in this channel: explicit home/ask binding, else the guild's only/first profile."""
        for cid in (channel_id, parent_channel_id):
            if cid is None:
                continue
            for p in self._by_id.values():
                if p.guild_id == guild_id and p.roles_of(cid) & {"home", "ask"}:
                    return p
        candidates = self.for_guild(guild_id)
        return min(candidates, key=lambda p: p.profile_id) if candidates else None

    def bump_epoch(self, profile_id: int, epoch: int) -> None:
        state = self._by_id.get(profile_id)
        if state is not None and epoch > state.knowledge_epoch:
            state.knowledge_epoch = epoch
