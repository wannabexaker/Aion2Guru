"""/settings panel (D-20): pick roles and channels from what exists in the server. Nothing hardcoded.

Every save creates a new config version (audited, reversible with /admin config-rollback).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import discord

from guru.core.config import ConfigError
from guru.core.permissions import Principal
from guru.services import settings_service as ss
from guru.services.profiles import ProfileState
from guru.store import actors

if TYPE_CHECKING:
    from guru.discord_bot.client import GuruBot

SECTIONS: list[tuple[str, str, str]] = [
    ("ai_users", "🤖 Who can talk to the AI", "Members without these roles get their messages removed"),
    ("contributors", "➕ Who can add knowledge", "/kb add, 📌, Add to knowledge"),
    ("moderators", "🛡️ Moderators", "Review channel votes, verify, edit"),
    ("admins", "⚙️ Admins", "Settings, sources, permissions"),
    ("trusted", "⭐ Trusted roles (team)", "Their information counts as reliable"),
    ("home", "🏠 Bot home channel", "The bot reads everything here"),
    ("mod_review", "🧾 Moderator review channel", "The bot asks the team here"),
    ("faq_publish", "📚 FAQ channel", "Forum (recommended) or text channel for approved FAQ"),
    ("access", "🚫 Unauthorized messages", "delete / ignore / notice"),
    ("limits", "⏱️ Rate limits", "Per user: minute / hour / day / AI answers"),
]


def _mentions(ids: list[int], kind: str) -> str:
    if not ids:
        return "—"
    prefix = "<@&" if kind == "role" else "<#"
    return " ".join(f"{prefix}{i}>" for i in ids)


class SettingsView(discord.ui.View):
    def __init__(self, bot: GuruBot, slug: str, principal: Principal) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.slug = slug
        self.principal = principal
        self.add_item(SectionSelect(self))

    @property
    def state(self) -> ProfileState:
        state = self.bot.registry.by_slug(self.slug)
        if state is None:
            raise ConfigError("profile not loaded")
        return state

    def config_dict(self) -> dict[str, Any]:
        return self.state.config.to_jsonable()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.principal.user_id

    def summary_embed(self) -> discord.Embed:
        cfg = self.state.config
        data = cfg.to_jsonable()
        e = discord.Embed(title=f"⚙️ {cfg.profile.name} — settings (v{self.state.version})", color=0x5865F2)
        for key in ("ai_users", "contributors", "moderators", "admins"):
            label = next(s[1] for s in SECTIONS if s[0] == key)
            e.add_field(name=label, value=_mentions(ss.group_roles(data, key), "role"), inline=False)
        trusted = [int(r) for r, t in data["trust"]["roles"].items() if t >= 3]
        e.add_field(name="⭐ Trusted roles", value=_mentions(trusted, "role"), inline=False)
        homes = [c.channel_id for c in cfg.channels_with_role("home")]
        mods = [c.channel_id for c in cfg.channels_with_role("mod_review")]
        faqs = [c.channel_id for c in cfg.channels_with_role("faq_publish")]
        e.add_field(name="🏠 Home", value=_mentions(homes, "channel"), inline=True)
        e.add_field(name="🧾 Review", value=_mentions(mods, "channel"), inline=True)
        e.add_field(name="📚 FAQ", value=_mentions(faqs, "channel"), inline=True)
        e.add_field(name="🚫 Unauthorized", value=cfg.access.unauthorized_action, inline=True)
        rl = cfg.rate_limits.default
        e.add_field(
            name="⏱️ Limits (min/hour/day/AI)",
            value=f"{rl.per_minute or '∞'} / {rl.per_hour or '∞'} / {rl.per_day or '∞'} / {rl.llm_per_day or '∞'}",
            inline=False,
        )
        if not cfg.access.bootstrap_admins:
            e.set_footer(text="Server administrators are NOT automatically bot admins.")
        return e

    async def apply(
        self, interaction: discord.Interaction, mutate: Callable[[dict[str, Any]], None], comment: str
    ) -> None:
        async with self.bot.rt.db.transaction() as conn:
            actor_id = await actors.discord_actor(conn, interaction.user.id)
        try:
            res = await self.bot.configs.patch(self.slug, mutate, actor_id, comment)
        except ConfigError as exc:
            await interaction.response.send_message(f"⚠️ {str(exc)[:1800]}", ephemeral=True)
            return
        await self.bot.registry.reload()
        fresh = SettingsView(self.bot, self.slug, self.principal)
        note = "No changes." if res.version is None else f"Saved as v{res.version}."
        await interaction.response.edit_message(content=note, embed=fresh.summary_embed(), view=fresh)

    async def back(self, interaction: discord.Interaction) -> None:
        fresh = SettingsView(self.bot, self.slug, self.principal)
        await interaction.response.edit_message(content=None, embed=fresh.summary_embed(), view=fresh)


class SectionSelect(discord.ui.Select["SettingsView"]):
    def __init__(self, parent: SettingsView) -> None:
        super().__init__(
            placeholder="What do you want to configure?",
            options=[discord.SelectOption(label=label, value=key, description=desc) for key, label, desc in SECTIONS],
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        p = self.panel
        resolver = p.state.resolver
        if key in ss.GROUPS:
            if not ss.can_edit_group(resolver, p.principal, key):
                await interaction.response.send_message(
                    "You need `perm.manage` and every permission of that group (no escalation).", ephemeral=True
                )
                return
            view: discord.ui.View = RoleGroupView(p, key)
        elif key == "trusted":
            if not resolver.can_assign_tier(p.principal, 3):
                await interaction.response.send_message("You need `perm.manage`.", ephemeral=True)
                return
            view = TrustedView(p)
        elif key in ("home", "mod_review", "faq_publish"):
            view = ChannelRoleView(p, key)
        elif key == "access":
            view = AccessView(p)
        else:
            await interaction.response.send_modal(LimitsModal(p))
            return
        label = next(s[1] for s in SECTIONS if s[0] == key)
        await interaction.response.edit_message(content=f"**{label}** — select and press Save.", view=view)


class _SubView(discord.ui.View):
    def __init__(self, parent: SettingsView) -> None:
        super().__init__(timeout=600)
        self.panel = parent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.panel.principal.user_id

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.panel.back(interaction)


class RoleGroupView(_SubView):
    def __init__(self, parent: SettingsView, group: str) -> None:
        super().__init__(parent)
        self.group = group
        current = ss.group_roles(parent.config_dict(), group)
        self.select: discord.ui.RoleSelect[RoleGroupView] = discord.ui.RoleSelect(
            placeholder="Roles",
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=r) for r in current][:25],
        )
        self.select.callback = self._noop  # type: ignore[method-assign]
        self.add_item(self.select)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        roles = [r.id for r in self.select.values]
        await self.panel.apply(interaction, ss.set_group_roles(self.group, roles), f"/settings {self.group}")


class TrustedView(_SubView):
    def __init__(self, parent: SettingsView) -> None:
        super().__init__(parent)
        data = parent.config_dict()
        current = [int(r) for r, t in data["trust"]["roles"].items() if t == 3]
        self.select: discord.ui.RoleSelect[TrustedView] = discord.ui.RoleSelect(
            placeholder="Trusted roles",
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=r) for r in current][:25],
        )
        self.select.callback = self._noop  # type: ignore[method-assign]
        self.add_item(self.select)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.panel.apply(
            interaction, ss.set_trusted_roles([r.id for r in self.select.values]), "/settings trusted"
        )


class ChannelRoleView(_SubView):
    def __init__(self, parent: SettingsView, role: str) -> None:
        super().__init__(parent)
        self.role = role
        current = [c.channel_id for c in parent.state.config.channels_with_role(role)]  # type: ignore[arg-type]
        self.select: discord.ui.ChannelSelect[ChannelRoleView] = discord.ui.ChannelSelect(
            placeholder="Channel",
            min_values=0,
            max_values=3 if role == "home" else 1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
            default_values=[discord.Object(id=c, type=discord.abc.GuildChannel) for c in current][:3],
        )
        self.select.callback = self._noop  # type: ignore[method-assign]
        self.add_item(self.select)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.panel.apply(
            interaction, ss.set_channel(self.role, [c.id for c in self.select.values]), f"/settings {self.role}"
        )


class AccessView(_SubView):
    def __init__(self, parent: SettingsView) -> None:
        super().__init__(parent)
        current = parent.state.config.access.unauthorized_action
        select: discord.ui.Select[AccessView] = discord.ui.Select(
            placeholder="When someone without access talks to the bot",
            options=[
                discord.SelectOption(label="Delete their message", value="delete", default=current == "delete"),
                discord.SelectOption(label="Delete + short notice", value="notice", default=current == "notice"),
                discord.SelectOption(label="Ignore", value="ignore", default=current == "ignore"),
            ],
        )

        async def on_select(interaction: discord.Interaction) -> None:
            await parent.apply(interaction, ss.set_access_action(select.values[0]), "/settings access")

        select.callback = on_select  # type: ignore[method-assign]
        self.add_item(select)


def _int_or_none(raw: str) -> int | None:
    raw = raw.strip()
    if raw in ("", "-", "∞", "0"):
        return None if raw != "0" else 0
    if not raw.isdigit():
        raise ValueError(f"not a number: {raw!r}")
    return int(raw)


class LimitsModal(discord.ui.Modal, title="Rate limits per user (empty = unlimited)"):
    def __init__(self, parent: SettingsView) -> None:
        super().__init__(timeout=600)
        self.panel = parent
        rl = parent.state.config.rate_limits.default
        self.per_minute: discord.ui.TextInput[LimitsModal] = discord.ui.TextInput(
            label="Questions per minute", default=str(rl.per_minute or ""), required=False, max_length=5
        )
        self.per_hour: discord.ui.TextInput[LimitsModal] = discord.ui.TextInput(
            label="Questions per hour", default=str(rl.per_hour or ""), required=False, max_length=5
        )
        self.per_day: discord.ui.TextInput[LimitsModal] = discord.ui.TextInput(
            label="Questions per day", default=str(rl.per_day or ""), required=False, max_length=6
        )
        self.llm_per_day: discord.ui.TextInput[LimitsModal] = discord.ui.TextInput(
            label="AI-generated answers per day", default=str(rl.llm_per_day or ""), required=False, max_length=6
        )
        for item in (self.per_minute, self.per_hour, self.per_day, self.llm_per_day):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            values = [
                _int_or_none(str(x.value)) for x in (self.per_minute, self.per_hour, self.per_day, self.llm_per_day)
            ]
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await self.panel.apply(interaction, ss.set_rate_limits(*values), "/settings limits")
