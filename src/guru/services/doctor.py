"""`guru doctor`: verify a deployment end to end and say exactly what to fix."""

from __future__ import annotations

import time
from dataclasses import dataclass

import asyncpg
import httpx

from guru.db.migrate import pending_migrations
from guru.llm.client import LLMUnavailable, build_llm_client
from guru.llm.embeddings import build_embedder
from guru.settings import Settings

GATEWAY_MESSAGE_CONTENT = 1 << 18
GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str = ""

    def line(self) -> str:
        icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[self.status]
        return f"{icon} {self.name}" + (f" — {self.detail}" if self.detail else "")


async def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    dsn = settings.database_url.get_secret_value()

    # Database
    try:
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            exts = {
                r["extname"]: r["extversion"] for r in await conn.fetch("SELECT extname, extversion FROM pg_extension")
            }
            pg = await conn.fetchval("SHOW server_version")
            checks.append(Check("PostgreSQL", "ok", f"server {pg}"))
            vec = exts.get("vector")
            checks.append(
                Check("pgvector", "ok" if vec else "fail", vec or "extension missing (use pgvector/pgvector image)")
            )
            profiles = (
                await conn.fetch(
                    """SELECT p.slug, v.config FROM profiles p
                         JOIN profile_config_versions v ON v.id = p.active_config_id"""
                )
                if await conn.fetchval("SELECT to_regclass('profiles') IS NOT NULL")
                else []
            )
        finally:
            await conn.close()
        pending = await pending_migrations(dsn)
        checks.append(
            Check(
                "migrations",
                "ok" if not pending else "warn",
                "up to date" if not pending else f"pending: {', '.join(pending)} → run `guru migrate`",
            )
        )
        if not profiles:
            checks.append(Check("profile", "warn", "none yet → in Discord run /setup template:aion2"))
        for p in profiles:
            roles = {c["role"] for c in p["config"].get("channels", [])}
            groups = p["config"].get("role_groups", {})
            missing = [r for r in ("home", "mod_review", "faq_publish") if r not in roles]
            detail = "channels ok" if not missing else f"not set: {', '.join(missing)} → /settings"
            if not groups.get("ai_users") and not groups.get("moderators"):
                detail += "; no role may talk to the AI yet (only server admins) → /settings"
            checks.append(Check(f"profile {p['slug']}", "ok" if not missing else "warn", detail))
    except (OSError, asyncpg.PostgresError) as exc:
        checks.append(Check("PostgreSQL", "fail", f"{type(exc).__name__}: {exc}"))

    async with httpx.AsyncClient(timeout=15) as client:
        # Discord token + privileged intent
        token = settings.discord_token.get_secret_value() if settings.discord_token else ""
        if not token:
            checks.append(Check("Discord token", "fail", "GURU_DISCORD_TOKEN not set"))
        else:
            try:
                r = await client.get(
                    "https://discord.com/api/v10/applications/@me", headers={"Authorization": f"Bot {token}"}
                )
                if r.status_code == 401:
                    checks.append(Check("Discord token", "fail", "invalid token"))
                else:
                    r.raise_for_status()
                    app = r.json()
                    flags = int(app.get("flags") or 0)
                    intent = bool(flags & (GATEWAY_MESSAGE_CONTENT | GATEWAY_MESSAGE_CONTENT_LIMITED))
                    checks.append(Check("Discord application", "ok", app.get("name", "")))
                    checks.append(
                        Check(
                            "Message Content intent",
                            "ok" if intent else "fail",
                            "enabled"
                            if intent
                            else "enable it: Developer Portal → Bot → Privileged Gateway Intents → Message Content",
                        )
                    )
            except httpx.HTTPError as exc:
                checks.append(Check("Discord API", "warn", f"unreachable: {exc}"))

        # Ollama / LLM
        for name, provider in settings.llm_providers.items():
            if provider.type != "ollama":
                continue
            try:
                r = await client.get(f"{provider.base_url.rstrip('/')}/api/tags")
                r.raise_for_status()
                models = {m["name"] for m in r.json().get("models", [])}
                checks.append(Check(f"Ollama ({name})", "ok", f"{len(models)} models"))
                for task, cfg in settings.llm_tasks.items():
                    if cfg.provider != name:
                        continue
                    present = cfg.model in models or f"{cfg.model}:latest" in models
                    checks.append(
                        Check(
                            f"model for {task}",
                            "ok" if present else "fail",
                            cfg.model if present else f"{cfg.model} missing → `ollama pull {cfg.model}`",
                        )
                    )
            except httpx.HTTPError as exc:
                checks.append(Check(f"Ollama ({name})", "fail", f"{provider.base_url}: {exc}"))
    if not settings.llm_tasks:
        checks.append(Check("LLM tasks", "warn", "none configured → answers/extraction stay deterministic only"))
    else:
        llm = build_llm_client(settings)
        for task in ("extract", "answer"):
            if not llm.enabled(task):
                checks.append(Check(f"LLM task {task}", "warn", "not configured"))
                continue
            t0 = time.perf_counter()
            try:
                res = await llm.run(
                    task,
                    "Reply with JSON.",
                    'Return {"ok": true}.',
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )
                ms = (time.perf_counter() - t0) * 1000
                checks.append(
                    Check(
                        f"LLM {task} JSON output",
                        "ok" if res.data.get("ok") is True else "warn",
                        f"{llm.model_of(task)} · {ms:.0f} ms",
                    )
                )
            except LLMUnavailable as exc:
                checks.append(Check(f"LLM {task}", "fail", str(exc)[:200]))
            except Exception as exc:  # bad output etc.
                checks.append(Check(f"LLM {task}", "warn", f"{type(exc).__name__}: {str(exc)[:200]}"))

    # Embeddings
    embedder = build_embedder(settings.embeddings)
    if embedder is None:
        checks.append(Check("embeddings", "warn", "disabled → lexical search only, no semantic dedupe"))
    else:
        try:
            vec = (await embedder.embed(["Fire Temple boss respawn"]))[0]
            checks.append(Check("embeddings", "ok", f"{embedder.name} · {len(vec)} dims"))
        except Exception as exc:
            checks.append(Check("embeddings", "fail", f"{embedder.name}: {str(exc)[:200]}"))

    # SearxNG
    if settings.web.searxng_url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{settings.web.searxng_url.rstrip('/')}/search", params={"q": "AION 2", "format": "json"}
                )
            ok = r.status_code == 200 and "results" in r.json()
            checks.append(
                Check(
                    "SearxNG",
                    "ok" if ok else "fail",
                    "json ok" if ok else f"HTTP {r.status_code} (enable `json` in search.formats)",
                )
            )
        except (httpx.HTTPError, ValueError) as exc:
            checks.append(Check("SearxNG", "fail", str(exc)[:200]))
    else:
        checks.append(Check("SearxNG", "warn", "web.searxng_url not set → source discovery off"))
    return checks
