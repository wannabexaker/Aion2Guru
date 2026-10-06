"""discord.py client: gateway events → MessageRouter, app commands, persistent views."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from guru.core.permissions import Principal
from guru.discord_bot.ui import FeedbackButton, principal_of, send_kwargs
from guru.logging import correlation, get_logger
from guru.services.config_service import ConfigService
from guru.services.knowledge_service import KnowledgeService
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryService
from guru.services.ratelimit_service import RateLimiter, user_hash
from guru.services.router import Delete, IncomingMessage, MessageRouter, Notice, Reply

if TYPE_CHECKING:
    from guru.app import Runtime

log = get_logger(__name__)


class GuruBot(discord.Client):
    def __init__(self, rt: Runtime) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # privileged: enable in the Developer Portal (R-01)
        intents.guild_reactions = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.rt = rt
        self.tree = app_commands.CommandTree(self)
        self.registry = ProfileRegistry(rt.db, frozenset(rt.settings.owner_ids))
        self.queries = QueryService(rt.db)
        self.knowledge = KnowledgeService(rt.db)
        self.configs = ConfigService(rt.db)
        self.salt = rt.settings.hash_salt.get_secret_value()
        self.limiter = RateLimiter(rt.db, self.salt)
        self.router: MessageRouter | None = None

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        from guru.discord_bot.commands import register_commands

        await self.registry.reload()
        await self.registry.listen(self.rt.dsn)
        assert self.user is not None
        self.router = MessageRouter(self.registry, self.queries, self.limiter, self.salt, self.user.id)
        self.add_dynamic_items(FeedbackButton)
        register_commands(self)
        if self.rt.settings.guild_ids:
            for gid in self.rt.settings.guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("bot.commands_synced", guilds=self.rt.settings.guild_ids or "global")

    async def on_ready(self) -> None:
        self.rt.ready["bot"] = True
        log.info("bot.ready", user=str(self.user), guilds=len(self.guilds))

    async def close(self) -> None:
        await self.registry.close()
        await super().close()

    # ------------------------------------------------------------ helpers
    def state_for(self, guild_id: int | None, channel: object) -> ProfileState | None:
        if guild_id is None:
            return None
        channel_id = getattr(channel, "id", 0)
        parent_id = getattr(channel, "parent_id", None)
        return self.registry.answering_profile(guild_id, channel_id, parent_id)

    @staticmethod
    def visible_restricted(
        member: discord.abc.User, guild: discord.Guild | None, state: ProfileState
    ) -> frozenset[int]:
        if guild is None or not isinstance(member, discord.Member):
            return frozenset()
        out: set[int] = set()
        for ch in state.config.channels:
            if ch.audience != "restricted":
                continue
            channel = guild.get_channel(ch.channel_id)
            if channel is not None and channel.permissions_for(member).view_channel:
                out.add(ch.channel_id)
        return frozenset(out)

    async def record_feedback(self, bot_message_id: int, user_id: int, value: int) -> bool:
        status = await self.rt.db.execute(
            "UPDATE query_log SET feedback = $3 WHERE bot_message_id = $1 AND user_hash = $2",
            bot_message_id,
            user_hash(self.salt, user_id),
            value,
        )
        return status.endswith(" 1")

    # ------------------------------------------------------------ events
    async def on_message(self, message: discord.Message) -> None:
        if self.router is None or self.user is None or message.author.id == self.user.id:
            return
        principal = principal_of(message.author)
        if message.webhook_id is not None:
            principal = Principal(user_id=message.author.id, is_bot=True, webhook_id=message.webhook_id)
        mentions_bot = self.user in message.mentions
        incoming = IncomingMessage(
            message_id=message.id,
            guild_id=message.guild.id if message.guild else None,
            channel_id=message.channel.id,
            parent_channel_id=getattr(message.channel, "parent_id", None),
            author=principal,
            content=message.content,
            mentions_bot=mentions_bot,
            created_at=message.created_at,
            reference_message_id=message.reference.message_id if message.reference else None,
            visible_channels=lambda state: self.visible_restricted(message.author, message.guild, state),
        )
        with correlation():
            try:
                if mentions_bot:
                    async with message.channel.typing():
                        result = await self.router.route(incoming)
                else:
                    result = await self.router.route(incoming)
            except Exception:
                log.error("router.failed", exc_info=True)
                return
            for action in result.actions:
                await self._execute(message, action)

    async def _execute(self, message: discord.Message, action: Reply | Delete | Notice) -> None:
        try:
            if isinstance(action, Reply):
                kwargs = send_kwargs(action.payload)
                if action.payload.delete_after:
                    kwargs["delete_after"] = action.payload.delete_after
                sent = await message.reply(mention_author=False, **kwargs)
                if action.on_sent is not None:
                    await action.on_sent(sent.id)
            elif isinstance(action, Delete):
                await message.delete()
            elif isinstance(action, Notice):
                kwargs = send_kwargs(action.payload)
                kwargs["allowed_mentions"] = discord.AllowedMentions(users=True)
                if action.payload.delete_after:
                    kwargs["delete_after"] = action.payload.delete_after
                await message.channel.send(**kwargs)
        except discord.Forbidden:
            log.warning(
                "discord.forbidden",
                action=type(action).__name__,
                channel=message.channel.id,
                hint="check bot permissions (Manage Messages for deletes)",
            )
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("discord.http_error", action=type(action).__name__, exc_info=True)


async def run_bot(rt: Runtime) -> None:
    token = rt.settings.discord_token
    if token is None:
        raise SystemExit("GURU_DISCORD_TOKEN is not set")
    bot = GuruBot(rt)
    task = asyncio.create_task(bot.start(token.get_secret_value()))
    stop = asyncio.create_task(rt.stop.wait())
    done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    await bot.close()
    if task in done and task.exception() is not None:
        raise task.exception()  # type: ignore[misc]
    stop.cancel()
