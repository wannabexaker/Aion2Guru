"""Applies per-user limits: minute in memory, hour/day in the DB (survive restarts)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from guru.core.permissions import Principal
from guru.core.ratelimit import MinuteWindow, effective_limits
from guru.db import Database
from guru.observability import metrics
from guru.services.profiles import ProfileState
from guru.store import usage


def user_hash(salt: str, user_id: int) -> bytes:
    return hashlib.sha256(f"{salt}:{user_id}".encode()).digest()


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    window: str | None = None  # 'minute' | 'hour' | 'day' | 'llm_day'
    retry_after_s: float | None = None


class _DayExceeded(Exception):
    """Abort the transaction so the hour unit consumed just before is given back."""


def _seconds_to_next(now: datetime, kind: str) -> float:
    if kind == "hour":
        return float(3600 - now.minute * 60 - now.second)
    return float(86400 - (now.hour * 3600 + now.minute * 60 + now.second))


class RateLimiter:
    def __init__(self, db: Database, salt: str) -> None:
        self.db = db
        self.salt = salt
        self.minute = MinuteWindow()

    def _limits(self, state: ProfileState, principal: Principal):  # type: ignore[no-untyped-def]
        return effective_limits(
            state.config.rate_limits, principal.role_ids, state.resolver.has(principal, "ratelimit.exempt")
        )

    async def take_query(self, state: ProfileState, principal: Principal, now: datetime | None = None) -> LimitDecision:
        limits = self._limits(state, principal)
        if limits is None:
            return LimitDecision(True)
        wait = self.minute.try_take((state.profile_id, principal.user_id), limits.per_minute)
        if wait:
            metrics.RATE_LIMITED.labels(bucket="minute").inc()
            return LimitDecision(False, "minute", wait)
        now = now or datetime.now(UTC)
        uh = user_hash(self.salt, principal.user_id)
        try:
            async with self.db.transaction() as conn:
                if not await usage.take(conn, state.profile_id, uh, "query", "hour", limits.per_hour, now):
                    metrics.RATE_LIMITED.labels(bucket="hour").inc()
                    return LimitDecision(False, "hour", _seconds_to_next(now, "hour"))
                if not await usage.take(conn, state.profile_id, uh, "query", "day", limits.per_day, now):
                    raise _DayExceeded
        except _DayExceeded:
            metrics.RATE_LIMITED.labels(bucket="day").inc()
            return LimitDecision(False, "day", _seconds_to_next(now, "day"))
        return LimitDecision(True)

    async def take_llm(self, state: ProfileState, principal: Principal, now: datetime | None = None) -> bool:
        limits = self._limits(state, principal)
        if limits is None:
            return True
        now = now or datetime.now(UTC)
        async with self.db.transaction() as conn:
            ok = await usage.take(
                conn, state.profile_id, user_hash(self.salt, principal.user_id), "llm", "day", limits.llm_per_day, now
            )
        if not ok:
            metrics.RATE_LIMITED.labels(bucket="llm_day").inc()
        return ok
