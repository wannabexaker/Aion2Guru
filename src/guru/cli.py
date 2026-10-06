"""Command line: guru migrate | run | config | jobs | audit."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from guru.settings import load_settings


def _run(coro: Coroutine[Any, Any, int]) -> int:
    return asyncio.run(coro)


async def _migrate() -> int:
    from guru.db.migrate import apply_migrations

    applied = await apply_migrations(load_settings().database_url.get_secret_value())
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    return 0


async def _config(args: argparse.Namespace) -> int:
    from guru.services import config_cli

    return await config_cli.main(args)


async def _jobs(args: argparse.Namespace) -> int:
    from guru.db import create_database
    from guru.jobs import queue

    db = await create_database(load_settings().database_url.get_secret_value(), migrate=False)
    try:
        async with db.connection() as conn:
            if args.retry:
                ok = await queue.retry_dead(conn, args.retry)
                print("re-queued" if ok else "not a dead job")
                return 0 if ok else 1
            for r in await queue.stats(conn):
                print(f"{r['kind']:<32} {r['status']:<8} {r['n']:>6}  oldest={r['oldest']:%Y-%m-%d %H:%M}")
    finally:
        await db.close()
    return 0


async def _audit(args: argparse.Namespace) -> int:
    from guru.db import create_database
    from guru.store import audit

    db = await create_database(load_settings().database_url.get_secret_value(), migrate=False)
    try:
        async with db.connection() as conn:
            if args.verify:
                broken = await audit.verify_chain(conn)
                print("audit chain intact" if broken is None else f"audit chain BROKEN at chain_seq={broken}")
                return 0 if broken is None else 2
            for r in await audit.query(conn, limit=args.limit):
                who = r["discord_user_id"] or r["actor_label"]
                print(
                    f"{r['chain_seq']:>6} {r['ts']:%Y-%m-%d %H:%M:%S} {r['action']:<28} "
                    f"{r['target_type'] or ''}:{r['target_id'] or ''} by {who}"
                )
    finally:
        await db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="guru")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply database migrations")

    run = sub.add_parser("run", help="run roles in this process")
    run.add_argument("--roles", default="bot,worker,api", help="comma separated: bot,worker,api")

    cfg = sub.add_parser("config", help="profile configuration")
    cfg_sub = cfg.add_subparsers(dest="config_cmd", required=True)
    v = cfg_sub.add_parser("validate", help="validate a profile YAML file")
    v.add_argument("file")
    a = cfg_sub.add_parser("apply", help="validate, diff and apply a profile YAML file")
    a.add_argument("file")
    a.add_argument("--yes", action="store_true", help="apply without confirmation")
    a.add_argument("--comment", default=None)
    e = cfg_sub.add_parser("export", help="export the active profile config as YAML")
    e.add_argument("slug")
    r = cfg_sub.add_parser("rollback", help="re-apply an older config version")
    r.add_argument("slug")
    r.add_argument("version", type=int)
    imp = cfg_sub.add_parser("import-entities", help="import entities/aliases from CSV")
    imp.add_argument("slug")
    imp.add_argument("file")

    jobs = sub.add_parser("jobs", help="job queue status")
    jobs.add_argument("--retry", type=int, default=None, help="re-queue a dead job id")

    au = sub.add_parser("audit", help="audit log")
    au.add_argument("--verify", action="store_true", help="verify the hash chain")
    au.add_argument("--limit", type=int, default=30)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "migrate":
        return _run(_migrate())
    if args.cmd == "run":
        from guru.app import run

        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        asyncio.run(run(load_settings(), roles))
        return 0
    if args.cmd == "config":
        return _run(_config(args))
    if args.cmd == "jobs":
        return _run(_jobs(args))
    if args.cmd == "audit":
        return _run(_audit(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
