"""Message routing (D-05, D-26, D-28): decides what to do with a Discord message. Platform-neutral.

Order is cost-ordered: ignore → access check (no DB) → rate limit → query pipeline.
Unauthorized users never cause DB or LLM work.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from guru.core.permissions import Principal
from guru.core.render import MessagePayload, render_answer, render_limited, tr
from guru.core.text import detect_language
from guru.logging import get_logger
from guru.observability import metrics
from guru.services.profiles import ProfileRegistry, ProfileState
from guru.services.query_service import Answer, QueryRequest, QueryService
from guru.services.ratelimit_service import RateLimiter, user_hash

log = get_logger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    guild_id: int | None
    channel_id: int
    parent_channel_id: int | None  # thread parent
    author: Principal
    content: str
    mentions_bot: bool
    created_at: datetime
    reference_message_id: int | None = None
    # Restricted channels (of the answering profile) this author can view; computed by the adapter.
    visible_channels: Callable[[ProfileState], frozenset[int]] = lambda _s: frozenset()


@dataclass(frozen=True)
class Reply:
    payload: MessagePayload
    # Called with the id of the sent bot message (query log, follow-ups, feedback).
    on_sent: Callable[[int | None], Awaitable[None]] | None = None


@dataclass(frozen=True)
class Delete:
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class Notice:
    channel_id: int
    payload: MessagePayload


Action = Reply | Delete | Notice


@dataclass
class RouteResult:
    actions: list[Action] = field(default_factory=list)
    reason: str = ""
    answer: Answer | None = None


HomeHandler = Callable[[ProfileState, IncomingMessage], Awaitable[None]]


def strip_mentions(content: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


class MessageRouter:
    def __init__(
        self,
        registry: ProfileRegistry,
        queries: QueryService,
        limiter: RateLimiter,
        salt: str,
        bot_user_id: int,
        on_home_message: HomeHandler | None = None,
    ) -> None:
        self.registry = registry
        self.queries = queries
        self.limiter = limiter
        self.salt = salt
        self.bot_user_id = bot_user_id
        self.on_home_message = on_home_message

    async def route(self, msg: IncomingMessage) -> RouteResult:
        if msg.guild_id is None:
            return RouteResult(reason="dm_ignored")  # D-22
        if msg.author.is_bot and msg.author.webhook_id is None:
            return RouteResult(reason="bot_ignored")
        state = self.registry.answering_profile(msg.guild_id, msg.channel_id, msg.parent_channel_id)
        if state is None:
            return RouteResult(reason="no_profile")
        in_home = bool(state.home_channel_ids & {msg.channel_id, msg.parent_channel_id or -1})
        if not (msg.mentions_bot or in_home):
            return RouteResult(reason="not_addressed")

        access = state.config.access
        enforce = (msg.mentions_bot and access.enforce_on_mention) or (in_home and access.enforce_in_home)
        if enforce and msg.author.webhook_id is None and not state.resolver.has(msg.author, "kb.query"):
            return self._deny(state, msg)

        if not msg.mentions_bot:
            # Home channel chatter from an authorized member → ingestion (M2), never an answer.
            if self.on_home_message is not None:
                await self.on_home_message(state, msg)
            return RouteResult(reason="home_ingest")

        text = strip_mentions(msg.content, self.bot_user_id)
        style = detect_language(text).style
        decision = await self.limiter.take_query(state, msg.author)
        if not decision.allowed:
            payload = render_limited(
                decision.window, decision.retry_after_s, style, delete_after=float(access.notice_seconds or 8)
            )
            return RouteResult([Reply(payload)], reason=f"limited:{decision.window}")

        req = QueryRequest(
            state=state,
            principal=msg.author,
            text=text,
            guild_id=msg.guild_id,
            channel_id=msg.channel_id,
            allowed_channels=msg.visible_channels(state),
            request_message_id=msg.message_id,
        )
        answer = await self.queries.answer(req)
        payload = render_answer(answer, state.config.profile.name)
        uh = user_hash(self.salt, msg.author.user_id)

        async def on_sent(bot_message_id: int | None) -> None:
            try:
                await self.queries.log(req, answer, user_hash=uh, bot_message_id=bot_message_id)
            except Exception:
                log.warning("query.log_failed", exc_info=True)

        return RouteResult([Reply(payload, on_sent)], reason=f"answered:{answer.mode}", answer=answer)

    def _deny(self, state: ProfileState, msg: IncomingMessage) -> RouteResult:
        action = state.config.access.unauthorized_action
        metrics.ACCESS_DENIED.labels(action=action).inc()
        log.info("access.denied", profile=state.slug, channel=msg.channel_id, action=action)
        if action == "ignore":
            return RouteResult(reason="denied:ignore")
        actions: list[Action] = [Delete(msg.channel_id, msg.message_id)]
        if action == "notice":
            style = detect_language(msg.content).style
            actions.append(
                Notice(
                    msg.channel_id,
                    MessagePayload(
                        content=f"<@{msg.author.user_id}> {tr('denied.notice', style)}",
                        delete_after=float(state.config.access.notice_seconds),
                    ),
                )
            )
        return RouteResult(actions, reason=f"denied:{action}")
