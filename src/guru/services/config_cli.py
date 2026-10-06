"""`guru config …` subcommands."""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from guru.core.config import ConfigError, load_yaml
from guru.core.text import normalize
from guru.db import create_database
from guru.services.config_service import ConfigService
from guru.settings import load_settings
from guru.store import actors, audit


async def main(args: argparse.Namespace) -> int:
    if args.config_cmd == "validate":
        try:
            cfg = load_yaml(await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8"))
        except ConfigError as exc:
            print(f"INVALID\n{exc}")
            return 2
        print(f"OK: profile {cfg.profile.slug!r}, {len(cfg.category_keys())} categories")
        return 0

    db = await create_database(load_settings().database_url.get_secret_value())
    svc = ConfigService(db)
    try:
        async with db.transaction() as conn:
            actor_id = await actors.system_actor(conn, "cli", kind="cli")
        if args.config_cmd == "apply":
            cfg = load_yaml(await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8"))
            diff = await svc.preview(cfg)
            if not diff:
                print("no changes")
                return 0
            print("\n".join(diff))
            if not args.yes and (await asyncio.to_thread(input, "apply? [y/N] ")).strip().lower() != "y":
                return 1
            res = await svc.apply(cfg, actor_id, comment=args.comment or f"cli apply {args.file}")
            print(f"applied {res.slug} v{res.version}")
        elif args.config_cmd == "export":
            print(await svc.export(args.slug))
        elif args.config_cmd == "rollback":
            res = await svc.rollback(args.slug, args.version, actor_id)
            print(f"rolled back {res.slug} to v{args.version} as v{res.version}")
        elif args.config_cmd == "import-entities":
            return await _import_entities(db, args.slug, Path(args.file), actor_id)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        await db.close()
    return 0


async def _import_entities(db, slug: str, path: Path, actor_id: int) -> int:  # type: ignore[no-untyped-def]
    """CSV columns: entity_type,canonical_name,aliases(| separated),category(optional)."""
    profile_id = await db.fetchval("SELECT id FROM profiles WHERE slug = $1", slug)
    if profile_id is None:
        raise ConfigError(f"unknown profile {slug!r}")
    types = {r["key"] for r in await db.fetch("SELECT key FROM entity_types WHERE profile_id = $1", profile_id)}
    created = aliases = skipped = 0
    async with db.transaction() as conn:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                etype = (row.get("entity_type") or "").strip()
                name = (row.get("canonical_name") or "").strip()
                if etype not in types or not name:
                    skipped += 1
                    continue
                entity_id = await conn.fetchval(
                    """INSERT INTO entities (profile_id, entity_type_key, canonical_name, category_key)
                       VALUES ($1, $2, $3, nullif($4, ''))
                       ON CONFLICT (profile_id, entity_type_key, canonical_name)
                       DO UPDATE SET category_key = coalesce(EXCLUDED.category_key, entities.category_key)
                       RETURNING id""",
                    profile_id,
                    etype,
                    name,
                    (row.get("category") or "").strip(),
                )
                created += 1
                for alias in {name, *[a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]}:
                    status = await conn.execute(
                        """INSERT INTO aliases (profile_id, target_type, target_key, alias, alias_norm, origin)
                           VALUES ($1, 'entity', $2, $3, $4, 'import') ON CONFLICT DO NOTHING""",
                        profile_id,
                        str(entity_id),
                        alias,
                        normalize(alias),
                    )
                    aliases += int(status.endswith(" 1"))
        await audit.record(
            conn,
            actor_id=actor_id,
            action="entity.import",
            profile_id=profile_id,
            target_type="profile",
            target_id=slug,
            after={"entities": created, "aliases": aliases, "skipped": skipped, "file": path.name},
        )
        await conn.execute("SELECT pg_notify('guru_config', $1)", slug)
    print(f"entities upserted={created} aliases added={aliases} skipped={skipped}")
    return 0
