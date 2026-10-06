"""Per-user rate limits (D-28). Minute window in memory; hour/day counters live in the DB."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from guru.core.config import RateLimitCfg, RateLimitRule


@dataclass(frozen=True)
class Limits:
    per_minute: int | None
    per_hour: int | None
    per_day: int | None
    llm_per_day: int | None


def _most_generous(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None  # None = unlimited
    return max(a, b)


def effective_limits(cfg: RateLimitCfg, role_ids: frozenset[int], exempt: bool) -> Limits | None:
    """None = exempt. Role overrides combine with the most generous value per window."""
    if exempt or role_ids & set(cfg.exempt_roles):
        return None
    overrides: list[RateLimitRule] = [cfg.roles[r] for r in role_ids if r in cfg.roles]
    rule = cfg.default
    if overrides:
        rule = overrides[0]
        for o in overrides[1:]:
            rule = RateLimitRule(
                per_minute=_most_generous(rule.per_minute, o.per_minute),
                per_hour=_most_generous(rule.per_hour, o.per_hour),
                per_day=_most_generous(rule.per_day, o.per_day),
                llm_per_day=_most_generous(rule.llm_per_day, o.llm_per_day),
            )
    return Limits(rule.per_minute, rule.per_hour, rule.per_day, rule.llm_per_day)


class MinuteWindow:
    """Sliding 60 s window per key. Single bot process → in-memory is correct (no Redis)."""

    def __init__(self) -> None:
        self._hits: dict[object, deque[float]] = {}
        self._now = time.monotonic

    def try_take(self, key: object, limit: int | None, now: float | None = None) -> float:
        """Returns 0 if allowed (and records the hit), else seconds until a slot frees up."""
        if limit is None:
            return 0.0
        t = self._now() if now is None else now
        q = self._hits.setdefault(key, deque())
        while q and t - q[0] >= 60:
            q.popleft()
        if len(q) >= limit:
            return max(0.1, 60 - (t - q[0]))
        q.append(t)
        if len(self._hits) > 50_000:  # bound memory: drop idle keys
            for k in [k for k, v in self._hits.items() if not v][:10_000]:
                del self._hits[k]
        return 0.0
