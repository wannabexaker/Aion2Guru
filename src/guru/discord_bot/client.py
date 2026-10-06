"""discord.py client: gateway events → services, app commands, persistent views, Discord-side jobs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from guru.core.permissions import Principal
from guru.core.render import COLOR, EmbedField, render_review, tr
from guru.discord_bot.ui import (
    EditStatementModal,
    FeedbackButton,
    ReviewButton,
    principal_of,
    respond_text,
    send_kwargs,
    to_embed,
)
from guru.jobs.queue import Job, PermanentJobError
from guru.jobs.worker import Worker, WorkerConfig
from guru.llm.client import build_llm_client
from guru.llm.embeddings import build_embedder
from guru.logging import correlation, get_logger
from guru.services.config_service import ConfigService
from guru.services.ingest_service import CapturedMessage, IngestService
from guru.services.knowledge_service import KnowledgeService, NotFound, PermissionDenied
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import QueryService
from guru.services.ratelimit_service import RateLimiter, user_hash
from guru.services.review_service import ReviewService
from guru.services.router import Delete, IncomingMessage, MessageRouter, Notice, Reply

if TYPE_CHECKING:
    from guru.app import Runtime

log = get_logger(__name__)


def _restricted(channel: Any, guild: discord.Guild | None) -> bool:
    """A channel @everyone cannot view → evidence from it is audience-restricted."""
    if guild is None or not hasattr(channel, "permissions_for"):
        return False
    target = channel.parent if isinstance(channel, discord.Thread) and channel.parent else channel
    try:
        return not target.permissions_for(guild.default_role).view_channel
    except Exception:
        return True  # fail closed


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
        self.salt = rt.settings.hash_salt.get_secret_value()
        self.limiter = RateLimiter(rt.db, self.salt)
        self.llm = build_llm_client(rt.settings)
        self.embedder = build_embedder(rt.settings.embeddings)
        self.queries = QueryService(rt.db, embedder=self.embedder, llm=self.llm, llm_quota=self.limiter.take_llm)
        self.knowledge = KnowledgeService(rt.db)
        self.configs = ConfigService(rt.db)
        self.ingest = IngestService(rt.db)
        self.reviews = ReviewService(rt.db)
        self.router: MessageRouter | None = None
        self._jobs_stop = asyncio.Event()
        self._jobs_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        from guru.discord_bot.commands import register_commands

        await self.registry.reload()
        await self.registry.listen(self.rt.dsn)
        assert self.user is not None
        self.router = MessageRouter(
            self.registry, self.queries, self.limiter, self.salt, self.user.id,
            on_home_message=self._home_ingest, ingest=self.ingest, review=self.reviews,
        )  # fmt: skip
        self.add_dynamic_items(FeedbackButton, ReviewButton)
        register_commands(self)
        if self.rt.settings.guild_ids:
            for gid in self.rt.settings.guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("bot.commands_synced", guilds=self.rt.settings.guild_ids or "global")
        # Discord side effects (review posts/edits) run here, where the gateway session lives.
        worker = Worker(
            self.rt.db,
            self.rt.dsn,
            {"discord.review_post": self._job_review_post, "discord.review_update": self._job_review_update},
            WorkerConfig(concurrency=2, poll_seconds=10),
        )
        self._jobs_task = asyncio.create_task(worker.run(self._jobs_stop))

    async def on_ready(self) -> None:
        self.rt.ready["bot"] = True
        log.info("bot.ready", user=str(self.user), guilds=len(self.guilds))

    async def close(self) -> None:
        self._jobs_stop.set()
        if self._jobs_task is not None:
            await asyncio.gather(self._jobs_task, return_exceptions=True)
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

    async def _channel(self, channel_id: int) -> Any:
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    @staticmethod
    def _review_style(state: ProfileState | None) -> str:
        return "el" if state is not None and "el" in state.config.profile.languages.accepted else "en"

    # ------------------------------------------------------------ messages
    async def _home_ingest(self, state: ProfileState, msg: IncomingMessage) -> None:
        await self.ingest.on_home_message(
            state,
            message_id=msg.message_id,
            channel_id=msg.channel_id,
            author=msg.author,
            content=msg.content,
            created_at=msg.created_at,
            reference_id=msg.reference_message_id,
            thread_id=msg.channel_id if msg.parent_channel_id else None,
        )

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
            channel_restricted=_restricted(message.channel, message.guild),
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

    # ------------------------------------------------------------ explicit capture
    async def capture_message(
        self, state: ProfileState, endorser: discord.Member, message: discord.Message, reason: str
    ) -> int:
        author_tier = (
            state.resolver.trust_tier(principal_of(message.author))
            if isinstance(message.author, discord.Member)
            else state.config.trust.default_member_tier
        )
        captured = CapturedMessage(
            message_id=message.id,
            guild_id=message.guild.id if message.guild else state.guild_id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_tier=author_tier,
            content=message.content,
            created_at=message.created_at,
            restricted=_restricted(message.channel, message.guild),
            thread_id=message.channel.id if isinstance(message.channel, discord.Thread) else None,
        )
        return await self.ingest.capture_explicit(state, principal_of(endorser), captured, reason=reason)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        state = self.registry.answering_profile(payload.guild_id, payload.channel_id)
        if state is None or str(payload.emoji) != state.config.ingestion.capture.reaction_emoji:
            return
        endorser = principal_of(payload.member)
        if state.resolver.trust_tier(endorser) < state.config.ingestion.capture.reaction_min_tier:
            return
        try:
            channel = await self._channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if not message.content.strip():
                return
            await self.capture_message(state, payload.member, message, "reaction")
            await message.add_reaction("📝")
        except PermissionDenied:
            return
        except discord.HTTPException:
            log.warning("capture.reaction_failed", exc_info=True)

    # ------------------------------------------------------------ edits & deletes (D-06)
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        content = payload.data.get("content")
        if content is None or payload.guild_id is None:
            return
        try:
            await self.ingest.on_edit(payload.channel_id, payload.message_id, content)
        except Exception:
            log.warning("sync.edit_failed", exc_info=True)

    def _purge(self, guild_id: int | None) -> bool:
        states = self.registry.for_guild(guild_id or 0)
        return any(s.config.retention.on_source_delete == "purge" for s in states)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        try:
            await self.ingest.on_delete(payload.channel_id, [payload.message_id], self._purge(payload.guild_id))
        except Exception:
            log.warning("sync.delete_failed", exc_info=True)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        try:
            await self.ingest.on_delete(payload.channel_id, list(payload.message_ids), self._purge(payload.guild_id))
        except Exception:
            log.warning("sync.delete_failed", exc_info=True)

    # ------------------------------------------------------------ moderator review (D-27)
    async def handle_review_click(
        self, interaction: discord.Interaction, task_id: int, decision: str, new_statement: str | None = None
    ) -> None:
        state = self.state_for(interaction.guild_id, interaction.channel)
        principal = principal_of(interaction.user)
        try:
            if state is None:
                raise NotFound("profile")
            if decision == "edit" and new_statement is None:
                if not state.resolver.has(principal, "review.vote"):
                    raise PermissionDenied("review.vote")
                task = await self.rt.db.fetchrow("SELECT payload FROM review_tasks WHERE id = $1", task_id)
                current = (task["payload"] or {}).get("statement", "") if task else ""
                await interaction.response.send_modal(EditStatementModal(self, task_id, current))
                return
            outcome = await self.reviews.vote(state, principal, task_id, decision, new_statement=new_statement)
        except PermissionDenied as exc:
            await respond_text(interaction, tr("perm.denied", self._review_style(state), capability=exc.capability))
            return
        except (NotFound, ValueError) as exc:
            await respond_text(interaction, f"⚠️ {exc}")
            return
        tally = " · ".join(f"{k}: {v}" for k, v in sorted(outcome.votes.items()))
        text = f"✔️ {outcome.decision} ({tally})" if outcome.closed else f"🗳️ {tally}"
        await respond_text(interaction, text)

    async def _job_review_post(self, job: Job) -> None:
        task = await self.rt.db.fetchrow("SELECT * FROM review_tasks WHERE id = $1", job.payload["task_id"])
        if task is None or task["status"] != "open" or task["discord_message_id"] is not None:
            return
        state = self.registry.get(task["profile_id"])
        if state is None or state.mod_review_channel_id is None:
            log.info("review.no_channel", task=task["id"])
            return
        try:
            channel = await self._channel(state.mod_review_channel_id)
            sent = await channel.send(**send_kwargs(render_review(dict(task), self._review_style(state))))
        except discord.Forbidden as exc:
            raise PermanentJobError("cannot post in the review channel (permissions)") from exc
        await self.rt.db.execute(
            "UPDATE review_tasks SET channel_id = $2, discord_message_id = $3 WHERE id = $1",
            task["id"],
            state.mod_review_channel_id,
            sent.id,
        )

    async def _job_review_update(self, job: Job) -> None:
        task = await self.rt.db.fetchrow(
            """SELECT t.*, a.discord_user_id AS closer FROM review_tasks t
                 LEFT JOIN actors a ON a.id = t.closed_by WHERE t.id = $1""",
            job.payload["task_id"],
        )
        if task is None or task["discord_message_id"] is None:
            return
        state = self.registry.get(task["profile_id"])
        style = self._review_style(state)
        payload = render_review(dict(task), style)
        if payload.embed is not None:
            decision = (task["outcome"] or {}).get("decision", task["status"])
            who = f"<@{task['closer']}>" if task["closer"] else "auto"
            payload.embed.color = COLOR["verified"] if decision in ("keep", "edit", "good") else COLOR["error"]
            payload.embed.fields.append(EmbedField("✔️", tr("review.decided", style, decision=decision, who=who)))
        try:
            channel = await self._channel(task["channel_id"])
            await channel.get_partial_message(task["discord_message_id"]).edit(embed=to_embed(payload), view=None)
        except discord.NotFound:
            return
        except discord.Forbidden as exc:
            raise PermanentJobError("cannot edit review message") from exc


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
