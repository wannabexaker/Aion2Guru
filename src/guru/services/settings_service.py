"""/settings mutations (D-20): role/channel based configuration, nothing hardcoded.

Each mutator edits a config dict; ConfigService.patch validates and applies it as a new version.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from guru.core.permissions import ROLE_GROUPS, PermissionResolver, Principal

GROUPS = ROLE_GROUPS

GROUP_LABELS = {
    "ai_users": "Who can talk to the AI",
    "contributors": "Who can add knowledge",
    "moderators": "Moderators (review, verify, edit)",
    "admins": "Admins (settings, sources, permissions)",
}

TRUST_LABELS = {3: "Trusted (team) — their info counts as reliable", 1: "Low trust", 0: "Blocked"}


def group_roles(cfg: dict[str, Any], group: str) -> list[int]:
    return sorted(int(r) for r in cfg.get("role_groups", {}).get(group, []))


def set_group_roles(group: str, role_ids: Iterable[int]) -> Callable[[dict[str, Any]], None]:
    """Make exactly `role_ids` members of the group. Capabilities are derived on apply."""
    if group not in ROLE_GROUPS:
        raise ValueError(f"unknown group {group!r}")
    roles = sorted({int(r) for r in role_ids})

    def mutate(cfg: dict[str, Any]) -> None:
        groups = cfg.setdefault("role_groups", {})
        if roles:
            groups[group] = roles
        else:
            groups.pop(group, None)

    return mutate


def set_trusted_roles(role_ids: Iterable[int], tier: int = 3) -> Callable[[dict[str, Any]], None]:
    ids = {int(r) for r in role_ids}

    def mutate(cfg: dict[str, Any]) -> None:
        roles = {int(k): v for k, v in cfg.setdefault("trust", {}).get("roles", {}).items() if v != tier}
        roles.update(dict.fromkeys(ids, tier))
        cfg["trust"]["roles"] = {str(k): v for k, v in sorted(roles.items())}

    return mutate


def set_channel(role: str, channel_ids: Iterable[int]) -> Callable[[dict[str, Any]], None]:
    """Replace all bindings of `role` (home, mod_review, ...) with the given channels."""
    ids = [int(c) for c in channel_ids]
    answering = {"home", "ask"}

    def mutate(cfg: dict[str, Any]) -> None:
        kept = []
        for c in cfg.get("channels", []):
            if c["role"] == role:
                continue
            if role in answering and c["role"] in answering and int(c["channel_id"]) in ids:
                continue  # a channel answers for one binding only
            kept.append(c)
        cfg["channels"] = kept + [{"channel_id": c, "role": role} for c in ids]

    return mutate


def set_access_action(action: str) -> Callable[[dict[str, Any]], None]:
    def mutate(cfg: dict[str, Any]) -> None:
        cfg.setdefault("access", {})["unauthorized_action"] = action

    return mutate


def set_rate_limits(
    per_minute: int | None, per_hour: int | None, per_day: int | None, llm_per_day: int | None
) -> Callable[[dict[str, Any]], None]:
    def mutate(cfg: dict[str, Any]) -> None:
        cfg.setdefault("rate_limits", {})["default"] = {
            "per_minute": per_minute,
            "per_hour": per_hour,
            "per_day": per_day,
            "llm_per_day": llm_per_day,
        }

    return mutate


def can_edit_group(resolver: PermissionResolver, who: Principal, group: str) -> bool:
    """No escalation: changing a group requires perm.manage and every capability of that group."""
    return all(resolver.can_grant(who, cap) for cap in GROUPS[group])
