"""Slash commands. Business rules live in services; this module only adapts Discord ↔ services."""

from __future__ import annotations

import contextlib
import io
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from guru.core.config import ConfigError, load_yaml
from guru.core.render import MessagePayload, render_answer, render_claim, render_limited, tr
from guru.core.text import detect_language
from guru.discord_bot.ui import principal_of, respond, respond_text, send_kwargs
from guru.jobs import queue
from guru.logging import correlation, get_logger
from guru.profiles import load_template, template_names
from guru.services.knowledge_service import NotFound, PermissionDenied
from guru.services.profiles import ProfileState
from guru.services.query_service import QueryRequest
from guru.services.ratelimit_service import user_hash
from guru.store import actors, audit

if TYPE_CHECKING:
    from guru.discord_bot.client import GuruBot

log = get_logger(__name__)

SCOPE_CHOICES = [
    app_commands.Choice(name="All knowledge", value="all"),
    app_commands.Choice(name="FAQ only", value="faq"),
    app_commands.Choice(name="Verified only", value="verified"),
    app_commands.Choice(name="Internal (Discord/team)", value="internal"),
    app_commands.Choice(name="Web knowledge", value="web"),
]
MAX_CONFIG_BYTES = 256 * 1024


def _style(interaction: discord.Interaction) -> str:
    return "el" if str(interaction.locale).startswith("el") else "en"


def _parse_claim_id(raw: str) -> int:
    s = raw.strip().upper().removeprefix("K-").removeprefix("K")
    if not s.isdigit():
        raise ValueError(f"invalid record id {raw!r}")
    return int(s)


class Ctx:
    """Resolved interaction context: profile + principal."""

    def __init__(self, bot: GuruBot, interaction: discord.Interaction) -> None:
        self.bot = bot
        self.interaction = interaction
        self.principal = principal_of(interaction.user)
        self.style = _style(interaction)
        self.state: ProfileState | None = bot.state_for(interaction.guild_id, interaction.channel)

    def require_state(self) -> ProfileState:
        if self.state is None:
            raise NotFound("no profile in this server — run /setup")
        return self.state

    def require(self, capability: str) -> ProfileState:
        state = self.require_state()
        if not state.resolver.has(self.principal, capability):
            raise PermissionDenied(capability)
        return state

    def visible(self) -> frozenset[int]:
        if self.state is None:
            return frozenset()
        return self.bot.visible_restricted(self.interaction.user, self.interaction.guild, self.state)

    async def actor_id(self) -> int:
        async with self.bot.rt.db.transaction() as conn:
            return await actors.discord_actor(conn, self.principal.user_id)


async def _handle_error(interaction: discord.Interaction, exc: Exception) -> None:
    style = _style(interaction)
    if isinstance(exc, PermissionDenied):
        text = tr("perm.denied", style, capability=exc.capability)
    elif isinstance(exc, NotFound):
        text = tr("notfound", style) + (f" ({exc.args[0]})" if exc.args and isinstance(exc.args[0], str) else "")
    elif isinstance(exc, (ConfigError, ValueError)):
        text = f"⚠️ {str(exc)[:1800]}"
    else:
        log.error("command.failed", command=interaction.command.name if interaction.command else None, exc_info=exc)
        text = "⚠️ Internal error (logged)."
    with contextlib.suppress(discord.HTTPException):
        await respond_text(interaction, text)


def register_commands(bot: GuruBot) -> None:
    tree = bot.tree

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await _handle_error(interaction, getattr(error, "original", error))

    async def category_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        state = bot.state_for(interaction.guild_id, interaction.channel)
        if state is None:
            return []
        cur = current.lower()
        out = []
        for cat, parent in state.config.flat_categories():
            label = f"{cat.name} ({cat.key})" if parent is None else f"↳ {cat.name} ({cat.key})"
            if cur in cat.key or cur in cat.name.lower():
                out.append(app_commands.Choice(name=label[:100], value=cat.key))
        return out[:25]

    # ---------------------------------------------------------------- /ask
    @tree.command(name="ask", description="Ask the knowledge base")
    @app_commands.guild_only()
    @app_commands.describe(
        question="Your question",
        scope="Where the answer may come from",
        category="Limit to a category",
        private="Only you see the answer",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(category=category_autocomplete)
    async def ask(
        interaction: discord.Interaction,
        question: str,
        scope: str | None = None,
        category: str | None = None,
        private: bool = False,
    ) -> None:
        with correlation():
            ctx = Ctx(bot, interaction)
            state = ctx.require("kb.query")
            style = detect_language(question).style
            decision = await bot.limiter.take_query(state, ctx.principal)
            if not decision.allowed:
                payload = render_limited(decision.window, decision.retry_after_s, style, delete_after=0)
                payload.ephemeral = True
                await respond(interaction, payload)
                return
            await interaction.response.defer(ephemeral=private, thinking=True)
            req = QueryRequest(
                state=state,
                principal=ctx.principal,
                text=question,
                guild_id=state.guild_id,
                channel_id=interaction.channel_id or 0,
                allowed_channels=ctx.visible(),
                scope_override=scope,
                category_override=category,
            )
            answer = await bot.queries.answer(req)
            payload = render_answer(answer, state.config.profile.name)
            sent = await interaction.followup.send(ephemeral=private, wait=True, **send_kwargs(payload))
            await bot.queries.log(
                req, answer, user_hash=user_hash(bot.salt, ctx.principal.user_id), bot_message_id=sent.id
            )

    # ---------------------------------------------------------------- /kb
    kb = app_commands.Group(name="kb", description="Knowledge base", guild_only=True)

    @kb.command(name="add", description="Add knowledge (you endorse it)")
    @app_commands.describe(
        statement="A self-contained fact, tip or procedure",
        category="Category",
        verified="Mark as verified (needs kb.verify)",
    )
    @app_commands.autocomplete(category=category_autocomplete)
    async def kb_add(
        interaction: discord.Interaction, statement: str, category: str | None = None, verified: bool = False
    ) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require_state()
        res = await bot.knowledge.add_manual(
            state,
            ctx.principal,
            statement,
            category_key=category,
            verify=verified,
            channel_id=interaction.channel_id,
            interaction_id=interaction.id,
        )
        verb = "Added" if res.created else "Linked to existing"
        await respond_text(interaction, f"{verb} `K-{res.claim_id}` — {res.verification}")

    @kb.command(name="show", description="Show a record with its sources")
    async def kb_show(interaction: discord.Interaction, record: str) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require("kb.query")
        data = await bot.knowledge.show(state, _parse_claim_id(record), set(ctx.visible()))
        await respond(interaction, render_claim(data, ctx.style))

    @kb.command(name="verify", description="Verify a record (moderators)")
    async def kb_verify(interaction: discord.Interaction, record: str) -> None:
        ctx = Ctx(bot, interaction)
        new_state = await bot.knowledge.verify(ctx.require_state(), ctx.principal, _parse_claim_id(record))
        await respond_text(interaction, f"`{record}` → {new_state}")

    @kb.command(name="retract", description="Retract a wrong record (moderators)")
    async def kb_retract(interaction: discord.Interaction, record: str, reason: str) -> None:
        ctx = Ctx(bot, interaction)
        await bot.knowledge.set_lifecycle(
            ctx.require_state(), ctx.principal, _parse_claim_id(record), "retracted", reason
        )
        await respond_text(interaction, f"`{record}` retracted")

    @kb.command(name="obsolete", description="Mark a record as no longer applicable (moderators)")
    async def kb_obsolete(interaction: discord.Interaction, record: str, reason: str) -> None:
        ctx = Ctx(bot, interaction)
        await bot.knowledge.set_lifecycle(
            ctx.require_state(), ctx.principal, _parse_claim_id(record), "obsolete", reason
        )
        await respond_text(interaction, f"`{record}` marked obsolete")

    tree.add_command(kb)

    # ---------------------------------------------------------------- /setup
    @tree.command(name="setup", description="Create the bot profile for this server from a template")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(template=[app_commands.Choice(name=n, value=n) for n in template_names()])
    async def setup(interaction: discord.Interaction, template: str) -> None:
        principal = principal_of(interaction.user)
        if not (principal.is_guild_admin or principal.user_id in bot.rt.settings.owner_ids):
            raise PermissionDenied("administrator")
        if bot.registry.for_guild(interaction.guild_id or 0):
            raise ValueError("this server already has a profile — use /settings")
        cfg = load_yaml(load_template(template))
        cfg = cfg.model_copy(update={"profile": cfg.profile.model_copy(update={"guild_id": interaction.guild_id})})
        ctx = Ctx(bot, interaction)
        res = await bot.configs.apply(
            cfg,
            await ctx.actor_id(),
            comment=f"/setup {template}",
            guild_name=interaction.guild.name if interaction.guild else None,
        )
        await bot.registry.reload()
        await respond_text(interaction, f"Profile `{res.slug}` created (v{res.version}). Next: `/settings`.")

    # ---------------------------------------------------------------- /settings
    @tree.command(name="settings", description="Configure the bot: roles, channels, access, limits")
    @app_commands.guild_only()
    async def settings(interaction: discord.Interaction) -> None:
        from guru.discord_bot.settings_view import SettingsView

        ctx = Ctx(bot, interaction)
        state = ctx.require("config.manage")
        view = SettingsView(bot, state.slug, ctx.principal)
        await interaction.response.send_message(embed=view.summary_embed(), view=view, ephemeral=True)

    # ---------------------------------------------------------------- /admin
    admin = app_commands.Group(name="admin", description="Administration", guild_only=True)

    @admin.command(name="config-export", description="Export the active profile config as YAML")
    async def config_export(interaction: discord.Interaction) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require("config.manage")
        text = await bot.configs.export(state.slug)
        file = discord.File(io.BytesIO(text.encode()), filename=f"{state.slug}-v{state.version}.yaml")
        await interaction.response.send_message(file=file, ephemeral=True)

    @admin.command(name="config-import", description="Validate, preview and apply a profile YAML")
    async def config_import(interaction: discord.Interaction, file: discord.Attachment) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require("config.manage")
        if file.size > MAX_CONFIG_BYTES:
            raise ValueError("file too large")
        cfg = load_yaml((await file.read()).decode("utf-8"))
        if cfg.profile.slug != state.slug:
            raise ValueError(f"slug mismatch: file is {cfg.profile.slug!r}, server profile is {state.slug!r}")
        cfg = cfg.model_copy(update={"profile": cfg.profile.model_copy(update={"guild_id": state.guild_id})})
        diff = await bot.configs.preview(cfg)
        if not diff:
            await respond_text(interaction, "No changes.")
            return
        preview = "\n".join(diff[:40]) + (f"\n… +{len(diff) - 40} more" if len(diff) > 40 else "")
        view = ConfirmView(ctx.principal.user_id)
        await interaction.response.send_message(f"```diff\n{preview[:1800]}\n```Apply?", view=view, ephemeral=True)
        if await view.wait() or not view.confirmed:
            return
        res = await bot.configs.apply(cfg, await ctx.actor_id(), comment=f"import {file.filename}")
        await bot.registry.reload()
        await interaction.followup.send(f"Applied v{res.version} ({len(res.diff)} changes).", ephemeral=True)

    @admin.command(name="config-rollback", description="Re-apply an older config version")
    async def config_rollback(interaction: discord.Interaction, version: int) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require("config.manage")
        res = await bot.configs.rollback(state.slug, version, await ctx.actor_id())
        await bot.registry.reload()
        await respond_text(interaction, f"Rolled back to v{version} as v{res.version}.")

    @admin.command(name="audit", description="Recent audit entries")
    async def audit_cmd(interaction: discord.Interaction, action: str | None = None, limit: int = 15) -> None:
        ctx = Ctx(bot, interaction)
        state = ctx.require("audit.read")
        async with bot.rt.db.connection() as conn:
            rows = await audit.query(conn, profile_id=state.profile_id, action_prefix=action, limit=min(limit, 30))
            broken = await audit.verify_chain(conn)
        lines = []
        for r in rows:
            who = f"<@{r['discord_user_id']}>" if r["discord_user_id"] else r["actor_label"]
            target = f"{r['target_type'] or ''}:{r['target_id'] or ''}"
            lines.append(f"`{r['chain_seq']}` {r['ts']:%m-%d %H:%M} **{r['action']}** {target} — {who}")
        chain = "🔗 chain intact" if broken is None else f"⛔ chain broken at {broken}"
        await respond(interaction, MessagePayload(content=(chain + "\n" + "\n".join(lines))[:1990], ephemeral=True))

    @admin.command(name="jobs", description="Job queue status")
    async def jobs_cmd(interaction: discord.Interaction) -> None:
        ctx = Ctx(bot, interaction)
        ctx.require("config.manage")
        async with bot.rt.db.connection() as conn:
            rows = await queue.stats(conn)
        lines = [f"`{r['kind']}` {r['status']}: {r['n']}" for r in rows] or ["queue empty"]
        await respond_text(interaction, "\n".join(lines)[:1990])

    tree.add_command(admin)


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="Applying…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()
