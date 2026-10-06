"""Conversion of platform-neutral payloads to discord.py objects + persistent buttons."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import discord

from guru.core.permissions import Principal
from guru.core.render import ButtonData, MessagePayload, tr
from guru.logging import get_logger

if TYPE_CHECKING:
    from guru.discord_bot.client import GuruBot

log = get_logger(__name__)

_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def principal_of(user: discord.abc.User) -> Principal:
    if isinstance(user, discord.Member):
        return Principal(
            user_id=user.id,
            role_ids=frozenset(r.id for r in user.roles),
            is_guild_admin=user.guild_permissions.administrator,
            is_bot=user.bot,
            account_created_at=user.created_at,
        )
    return Principal(user_id=user.id, is_bot=user.bot, account_created_at=getattr(user, "created_at", None))


def to_embed(payload: MessagePayload) -> discord.Embed | None:
    e = payload.embed
    if e is None:
        return None
    embed = discord.Embed(description=e.description, color=e.color, title=e.title)
    for f in e.fields:
        embed.add_field(name=f.name, value=f.value or "—", inline=f.inline)
    if e.footer:
        embed.set_footer(text=e.footer)
    return embed


def to_view(buttons: list[ButtonData]) -> discord.ui.View | None:
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for b in buttons:
        view.add_item(
            discord.ui.Button(
                custom_id=b.custom_id,
                label=b.label or None,
                emoji=b.emoji,
                style=_STYLES.get(b.style, discord.ButtonStyle.secondary),
            )
        )
    return view


def send_kwargs(payload: MessagePayload) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
    if payload.content is not None:
        kwargs["content"] = payload.content
    embed = to_embed(payload)
    if embed is not None:
        kwargs["embed"] = embed
    view = to_view(payload.buttons)
    if view is not None:
        kwargs["view"] = view
    return kwargs


async def respond(interaction: discord.Interaction, payload: MessagePayload) -> None:
    kwargs = send_kwargs(payload)
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=payload.ephemeral, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=payload.ephemeral, **kwargs)


async def respond_text(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
    await respond(interaction, MessagePayload(content=text, ephemeral=ephemeral))


class FeedbackButton(discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=r"guru:fb:(?P<v>up|down)"):
    """👍/👎 on answers. Persistent across restarts; only the asker's vote counts."""

    def __init__(self, value: str) -> None:
        super().__init__(discord.ui.Button(custom_id=f"guru:fb:{value}", emoji="👍" if value == "up" else "👎"))
        self.value = value

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str]
    ) -> FeedbackButton:
        return cls(match["v"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: GuruBot = interaction.client  # type: ignore[assignment]
        if interaction.message is None:
            return
        ok = await bot.record_feedback(interaction.message.id, interaction.user.id, 1 if self.value == "up" else -1)
        style = "el" if (interaction.locale and str(interaction.locale).startswith("el")) else "en"
        text = tr("feedback.thanks", style) if ok else "—"
        await interaction.response.send_message(text, ephemeral=True)
