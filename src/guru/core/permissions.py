"""Capabilities, trust tiers and their resolution. Pure, no I/O.

Rules:
- allow-only grants (no deny) → fewer surprises
- bootstrap: Discord Administrator / owner ids hold every capability (break-glass, configurable)
- no escalation: nobody can grant a capability or trust tier they do not hold themselves
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

Capability = Literal[
    "kb.query",
    "kb.query.live_web",
    "kb.ingest",
    "kb.verify",
    "kb.edit",
    "kb.conflict.resolve",
    "review.vote",
    "faq.approve",
    "faq.manage",
    "rules.manage",
    "sources.manage",
    "config.manage",
    "perm.manage",
    "audit.read",
    "ratelimit.exempt",
]

ALL_CAPABILITIES: tuple[str, ...] = Capability.__args__  # type: ignore[attr-defined]

GroupName = Literal["ai_users", "contributors", "moderators", "admins"]

# Role groups managed from /settings. A role in a group holds all the group's capabilities.
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "ai_users": ("kb.query",),
    "contributors": ("kb.query", "kb.ingest"),
    "moderators": (
        "kb.query", "kb.ingest", "kb.verify", "kb.edit", "kb.conflict.resolve", "review.vote", "faq.approve",
        "audit.read", "ratelimit.exempt",
    ),
    "admins": ALL_CAPABILITIES,
}  # fmt: skip

TIER_NAMES = {0: "blocked", 1: "low", 2: "community", 3: "trusted", 4: "official"}
MAX_TIER = 4


@dataclass(frozen=True)
class Principal:
    """Who is acting, as seen by Discord at this moment."""

    user_id: int
    role_ids: frozenset[int] = frozenset()
    is_guild_admin: bool = False
    is_bot: bool = False
    webhook_id: int | None = None
    account_created_at: datetime | None = None


@dataclass(frozen=True)
class Grant:
    capability: str
    subject_type: Literal["role", "user", "everyone"]
    subject_id: int = 0


@dataclass(frozen=True)
class TrustRules:
    default_member_tier: int = 2
    new_account_days: int = 14
    roles: dict[int, int] = field(default_factory=dict)
    users: dict[int, int] = field(default_factory=dict)
    webhooks: dict[int, int] = field(default_factory=dict)


class PermissionResolver:
    def __init__(
        self,
        grants: list[Grant],
        trust: TrustRules,
        owner_ids: frozenset[int] = frozenset(),
        bootstrap_admins: bool = True,
    ) -> None:
        self._trust = trust
        self._owner_ids = owner_ids
        self._bootstrap = bootstrap_admins
        self._everyone: set[str] = set()
        self._by_role: dict[int, set[str]] = {}
        self._by_user: dict[int, set[str]] = {}
        for g in grants:
            if g.subject_type == "everyone":
                self._everyone.add(g.capability)
            elif g.subject_type == "role":
                self._by_role.setdefault(g.subject_id, set()).add(g.capability)
            else:
                self._by_user.setdefault(g.subject_id, set()).add(g.capability)

    def is_superuser(self, p: Principal) -> bool:
        return p.user_id in self._owner_ids or (self._bootstrap and p.is_guild_admin)

    def capabilities(self, p: Principal) -> frozenset[str]:
        if p.is_bot and p.webhook_id is None:
            return frozenset()
        if self.is_superuser(p):
            return frozenset(ALL_CAPABILITIES)
        caps = set(self._everyone)
        caps |= self._by_user.get(p.user_id, set())
        for role in p.role_ids:
            caps |= self._by_role.get(role, set())
        return frozenset(caps)

    def has(self, p: Principal, capability: str) -> bool:
        return capability in self.capabilities(p)

    def trust_tier(self, p: Principal, now: datetime | None = None) -> int:
        if p.webhook_id is not None:
            return self._trust.webhooks.get(p.webhook_id, 1)
        if p.user_id in self._trust.users:
            return self._trust.users[p.user_id]
        tier = max((self._trust.roles[r] for r in p.role_ids if r in self._trust.roles), default=None)
        if tier is None:
            tier = self._trust.default_member_tier
            if p.account_created_at is not None and self._trust.new_account_days > 0:
                now = now or datetime.now(UTC)
                if now - p.account_created_at < timedelta(days=self._trust.new_account_days):
                    tier = min(tier, 1)
        if self.is_superuser(p):
            tier = max(tier, 3)
        return tier

    def can_grant(self, granter: Principal, capability: str) -> bool:
        """No escalation: requires perm.manage and holding the capability being granted."""
        caps = self.capabilities(granter)
        return "perm.manage" in caps and capability in caps

    def can_assign_tier(self, granter: Principal, tier: int) -> bool:
        if "perm.manage" not in self.capabilities(granter):
            return False
        if tier >= MAX_TIER:  # 'official' only by superusers
            return self.is_superuser(granter)
        return tier <= max(self.trust_tier(granter), 3)
