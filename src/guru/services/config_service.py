"""Profile configuration lifecycle: validate → diff → apply (versioned) → materialize → notify.

The DB is the source of truth (D-12). Every apply creates a new immutable version; rollback
re-applies an old snapshot as a new version. Materialization upserts tables that other rows
reference (categories, sources) so ids stay stable across applies.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from guru.core.config import ConfigError, ProfileConfig, diff_configs, dump_yaml, parse_config
from guru.core.permissions import ROLE_GROUPS
from guru.core.text import normalize
from guru.db import Database
from guru.store import audit

CONFIG_NOTIFY_CHANNEL = "guru_config"


@dataclass(frozen=True)
class ApplyResult:
    profile_id: int
    slug: str
    version: int | None  # None → identical to active config, nothing applied
    diff: list[str]


class ConfigService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ reads
    async def load_active(self, slug: str) -> tuple[int, int, ProfileConfig]:
        row = await self.db.fetchrow(
            """SELECT p.id, v.version, v.config FROM profiles p
                 JOIN profile_config_versions v ON v.id = p.active_config_id
                WHERE p.slug = $1""",
            slug,
        )
        if row is None:
            raise ConfigError(f"profile {slug!r} has no active config")
        return row["id"], row["version"], parse_config(row["config"])

    async def export(self, slug: str) -> str:
        _, _, cfg = await self.load_active(slug)
        return dump_yaml(cfg)

    async def versions(self, slug: str, limit: int = 20) -> list[asyncpg.Record]:
        return await self.db.fetch(
            """SELECT v.version, v.created_at, v.comment, a.discord_user_id, a.label
                 FROM profile_config_versions v
                 JOIN profiles p ON p.id = v.profile_id
                 JOIN actors a ON a.id = v.created_by
                WHERE p.slug = $1 ORDER BY v.version DESC LIMIT $2""",
            slug,
            limit,
        )

    async def preview(self, cfg: ProfileConfig) -> list[str]:
        row = await self.db.fetchrow(
            """SELECT v.config FROM profiles p JOIN profile_config_versions v ON v.id = p.active_config_id
                WHERE p.slug = $1""",
            cfg.profile.slug,
        )
        return diff_configs(None if row is None else row["config"], cfg.to_jsonable())

    # ------------------------------------------------------------ writes
    async def apply(
        self, cfg: ProfileConfig, actor_id: int, comment: str | None = None, guild_name: str | None = None
    ) -> ApplyResult:
        async with self.db.transaction() as conn:
            return await self._apply(conn, cfg, actor_id, comment, guild_name)

    async def patch(
        self, slug: str, mutate: Callable[[dict[str, Any]], None], actor_id: int, comment: str
    ) -> ApplyResult:
        """Load active config, mutate a deep copy, re-validate and apply (used by /settings)."""
        async with self.db.transaction() as conn:
            row = await conn.fetchrow(
                """SELECT v.config FROM profiles p JOIN profile_config_versions v ON v.id = p.active_config_id
                    WHERE p.slug = $1 FOR UPDATE OF p""",
                slug,
            )
            if row is None:
                raise ConfigError(f"profile {slug!r} has no active config")
            data = copy.deepcopy(row["config"])
            mutate(data)
            return await self._apply(conn, parse_config(data), actor_id, comment, None)

    async def rollback(self, slug: str, version: int, actor_id: int) -> ApplyResult:
        row = await self.db.fetchrow(
            """SELECT v.config FROM profile_config_versions v JOIN profiles p ON p.id = v.profile_id
                WHERE p.slug = $1 AND v.version = $2""",
            slug,
            version,
        )
        if row is None:
            raise ConfigError(f"profile {slug!r} has no version {version}")
        return await self.apply(parse_config(row["config"]), actor_id, comment=f"rollback to v{version}")

    async def _apply(
        self,
        conn: asyncpg.Connection,
        cfg: ProfileConfig,
        actor_id: int,
        comment: str | None,
        guild_name: str | None,
    ) -> ApplyResult:
        slug = cfg.profile.slug
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('guru_config:' || $1))", slug)
        profile = await conn.fetchrow("SELECT id, guild_id, active_config_id FROM profiles WHERE slug = $1", slug)
        guild_id = cfg.profile.guild_id or (profile["guild_id"] if profile else None)
        if guild_id is None:
            raise ConfigError("profile.guild_id is required for a new profile")
        if profile is not None and profile["guild_id"] != guild_id:
            raise ConfigError(f"profile {slug!r} belongs to another guild")
        # The stored config always carries the guild id.
        cfg = cfg.model_copy(update={"profile": cfg.profile.model_copy(update={"guild_id": guild_id})})

        await conn.execute(
            """INSERT INTO guilds (guild_id, name) VALUES ($1, $2)
               ON CONFLICT (guild_id) DO UPDATE SET name = coalesce($3, guilds.name)""",
            guild_id,
            guild_name or str(guild_id),
            guild_name,
        )
        if profile is None:
            profile_id = await conn.fetchval(
                "INSERT INTO profiles (slug, name, guild_id) VALUES ($1, $2, $3) RETURNING id",
                slug,
                cfg.profile.name,
                guild_id,
            )
            old: dict[str, Any] | None = None
            old_hash = None
        else:
            profile_id = profile["id"]
            prev = await conn.fetchrow(
                "SELECT config, config_hash FROM profile_config_versions WHERE id = $1", profile["active_config_id"]
            )
            old = None if prev is None else prev["config"]
            old_hash = None if prev is None else bytes(prev["config_hash"])

        new_hash = cfg.config_hash()
        if old_hash == new_hash:
            return ApplyResult(profile_id, slug, None, [])

        diff = diff_configs(old, cfg.to_jsonable())
        version = await conn.fetchval(
            "SELECT coalesce(max(version), 0) + 1 FROM profile_config_versions WHERE profile_id = $1", profile_id
        )
        version_id = await conn.fetchval(
            """INSERT INTO profile_config_versions
                 (profile_id, version, schema_version, config, config_hash, created_by, comment)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            profile_id,
            version,
            cfg.schema_version,
            cfg.to_jsonable(),
            new_hash,
            actor_id,
            comment,
        )
        await materialize(conn, profile_id, guild_id, cfg)
        await conn.execute(
            "UPDATE profiles SET active_config_id = $2, name = $3 WHERE id = $1",
            profile_id,
            version_id,
            cfg.profile.name,
        )
        await conn.execute(
            "UPDATE guilds SET default_profile_id = coalesce(default_profile_id, $2) WHERE guild_id = $1",
            guild_id,
            profile_id,
        )
        await audit.record(
            conn,
            actor_id=actor_id,
            action="config.apply",
            profile_id=profile_id,
            target_type="profile",
            target_id=slug,
            after={"version": version, "diff": diff[:200], "diff_size": len(diff)},
            reason=comment,
        )
        await conn.execute("SELECT pg_notify($1, $2)", CONFIG_NOTIFY_CHANNEL, slug)
        return ApplyResult(profile_id, slug, version, diff)


async def materialize(conn: asyncpg.Connection, profile_id: int, guild_id: int, cfg: ProfileConfig) -> None:
    # Categories: upsert by key (claims reference them), disable removed ones.
    cat_ids: dict[str, int] = {}
    for cat, parent in cfg.flat_categories():
        cat_ids[cat.key] = await conn.fetchval(
            """INSERT INTO categories (profile_id, key, parent_id, name, description, settings, enabled)
               VALUES ($1, $2, $3, $4, $5, $6, true)
               ON CONFLICT (profile_id, key) DO UPDATE
                 SET parent_id = EXCLUDED.parent_id, name = EXCLUDED.name, description = EXCLUDED.description,
                     settings = EXCLUDED.settings, enabled = true
               RETURNING id""",
            profile_id,
            cat.key,
            cat_ids.get(parent) if parent else None,
            cat.name,
            cat.description,
            cat.settings.model_dump(mode="json"),
        )
    await conn.execute(
        "UPDATE categories SET enabled = false WHERE profile_id = $1 AND NOT (key = ANY($2::text[]))",
        profile_id,
        list(cat_ids),
    )

    await conn.execute(
        "DELETE FROM entity_types WHERE profile_id = $1 AND NOT (key = ANY($2::text[]))",
        profile_id,
        [e.key for e in cfg.entity_types],
    )
    for et in cfg.entity_types:
        await conn.execute(
            """INSERT INTO entity_types (profile_id, key, name, default_category_id, attribute_schema)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (profile_id, key) DO UPDATE
                 SET name = EXCLUDED.name, default_category_id = EXCLUDED.default_category_id,
                     attribute_schema = EXCLUDED.attribute_schema""",
            profile_id,
            et.key,
            et.name,
            cat_ids.get(et.default_category) if et.default_category else None,
            {k: v.model_dump(mode="json", by_alias=True) for k, v in et.attributes.items()},
        )

    await conn.execute("DELETE FROM intents WHERE profile_id = $1", profile_id)
    for it in cfg.intents:
        await conn.execute(
            """INSERT INTO intents (profile_id, key, description, target, patterns, examples)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            profile_id,
            it.key,
            it.description,
            it.target,
            it.patterns,
            it.examples,
        )

    await conn.execute("DELETE FROM dimension_values WHERE profile_id = $1", profile_id)
    for dim_key, dim in cfg.dimensions.items():
        for ordinal, dv in enumerate(dim.values):
            await conn.execute(
                """INSERT INTO dimension_values (profile_id, dimension, value, ordinal, valid_from, scope)
                   VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING""",
                profile_id,
                dim_key,
                dv.value,
                ordinal if dim.type == "ordered" else None,
                dv.valid_from,
                dv.scope,
            )

    await conn.execute("DELETE FROM channel_bindings WHERE profile_id = $1", profile_id)
    for ch in cfg.channels:
        await conn.execute(
            """INSERT INTO channel_bindings
                 (profile_id, guild_id, channel_id, role, default_category_id, audience, settings)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            profile_id,
            guild_id,
            ch.channel_id,
            ch.role,
            cat_ids.get(ch.default_category) if ch.default_category else None,
            ch.audience,
            {"ingest": ch.ingest.model_dump(mode="json")},
        )

    # System sources every profile needs (manual entries, Discord). Never disabled by config.
    for key, kind, name, group, tier in (
        ("manual", "manual", "Manual entries", "manual", 3),
        ("import", "import", "Bulk imports", "import", 3),
    ):
        await conn.execute(
            """INSERT INTO sources (profile_id, key, kind, name, independence_group, trust_tier, origin)
               VALUES ($1, $2, $3, $4, $5, $6, 'system') ON CONFLICT (profile_id, key) DO NOTHING""",
            profile_id,
            key,
            kind,
            name,
            group,
            tier,
        )
    for src in cfg.sources:
        await conn.execute(
            """INSERT INTO sources (profile_id, key, kind, name, locator, independence_group, trust_tier,
                                    trust_pinned, origin, fetch_config, schedule_seconds, enabled, next_fetch_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'config', $9, $10, true, now())
               ON CONFLICT (profile_id, key) DO UPDATE
                 SET kind = EXCLUDED.kind, name = EXCLUDED.name, locator = EXCLUDED.locator,
                     independence_group = EXCLUDED.independence_group, trust_tier = EXCLUDED.trust_tier,
                     trust_pinned = EXCLUDED.trust_pinned, fetch_config = EXCLUDED.fetch_config,
                     schedule_seconds = EXCLUDED.schedule_seconds, enabled = true""",
            profile_id,
            src.key,
            src.kind,
            src.name or src.key,
            src.locator,
            src.independence_group,
            src.trust_tier,
            src.trust_pinned,
            src.fetch_config,
            src.schedule_seconds,
        )
    await conn.execute(
        """UPDATE sources SET enabled = false
            WHERE profile_id = $1 AND origin = 'config' AND NOT (key = ANY($2::text[]))""",
        profile_id,
        [s.key for s in cfg.sources],
    )

    await conn.execute("DELETE FROM permission_grants WHERE profile_id = $1", profile_id)
    for perm in cfg.permissions:
        subjects: list[tuple[str, int]] = [("role", r) for r in perm.roles] + [("user", u) for u in perm.users]
        if perm.everyone:
            subjects.append(("everyone", 0))
        for subject_type, subject_id in subjects:
            await conn.execute(
                """INSERT INTO permission_grants (profile_id, subject_type, subject_id, capability)
                   VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING""",
                profile_id,
                subject_type,
                subject_id,
                perm.capability,
            )

    for group, role_ids in cfg.role_groups.items():
        for role_id in role_ids:
            for cap in ROLE_GROUPS[group]:
                await conn.execute(
                    """INSERT INTO permission_grants (profile_id, subject_type, subject_id, capability)
                       VALUES ($1, 'role', $2, $3) ON CONFLICT DO NOTHING""",
                    profile_id,
                    role_id,
                    cap,
                )

    await conn.execute("DELETE FROM trust_assignments WHERE profile_id = $1", profile_id)
    for subject_type, mapping in (
        ("role", cfg.trust.roles),
        ("user", cfg.trust.users),
        ("webhook", cfg.trust.webhooks),
    ):
        for subject_id, tier in mapping.items():
            await conn.execute(
                """INSERT INTO trust_assignments (profile_id, subject_type, subject_id, trust_tier)
                   VALUES ($1, $2, $3, $4)""",
                profile_id,
                subject_type,
                subject_id,
                tier,
            )

    await conn.execute("DELETE FROM aliases WHERE profile_id = $1 AND origin = 'config'", profile_id)
    alias_rows: list[tuple[str, str, str]] = [("profile", "", kw) for kw in cfg.profile.keywords]
    for cat, _ in cfg.flat_categories():
        alias_rows += [("category", cat.key, a) for a in (*cat.keywords, *cat.aliases)]
    for target_type, target_key, alias in alias_rows:
        norm = normalize(alias)
        if not norm:
            continue
        await conn.execute(
            """INSERT INTO aliases (profile_id, target_type, target_key, alias, alias_norm, origin)
               VALUES ($1, $2, $3, $4, $5, 'config') ON CONFLICT DO NOTHING""",
            profile_id,
            target_type,
            target_key,
            alias,
            norm,
        )
