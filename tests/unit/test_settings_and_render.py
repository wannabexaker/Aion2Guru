from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from guru.core.config import parse_config
from guru.core.permissions import Grant, PermissionResolver, Principal, TrustRules
from guru.core.render import render_answer, render_limited, tr
from guru.services import settings_service as ss


def _cfg() -> dict[str, Any]:
    return {
        "profile": {"slug": "test", "name": "T"},
        "categories": [{"key": "general", "name": "General"}],
        "permissions": [],
        "trust": {"roles": {}},
        "channels": [],
    }


def test_group_roles_roundtrip() -> None:
    cfg = _cfg()
    ss.set_group_roles("moderators", [10, 11])(cfg)
    ss.set_group_roles("admins", [11])(cfg)
    assert ss.group_roles(cfg, "moderators") == [10, 11]
    ss.set_group_roles("moderators", [10])(cfg)
    assert ss.group_roles(cfg, "moderators") == [10] and ss.group_roles(cfg, "admins") == [11]
    ss.set_group_roles("admins", [])(cfg)
    assert ss.group_roles(cfg, "admins") == [] and "admins" not in cfg["role_groups"]
    assert parse_config(cfg).role_groups == {"moderators": [10]}


def test_channels_and_access_and_limits() -> None:
    cfg = _cfg()
    ss.set_channel("home", [5])(cfg)
    ss.set_channel("mod_review", [6])(cfg)
    ss.set_channel("home", [7])(cfg)
    assert sorted((c["channel_id"], c["role"]) for c in cfg["channels"]) == [(6, "mod_review"), (7, "home")]
    ss.set_access_action("notice")(cfg)
    ss.set_rate_limits(5, None, 100, 10)(cfg)
    parsed = parse_config(cfg)
    assert parsed.access.unauthorized_action == "notice"
    assert parsed.rate_limits.default.per_hour is None and parsed.rate_limits.default.per_day == 100


def test_trusted_roles() -> None:
    cfg = _cfg()
    ss.set_trusted_roles([1, 2])(cfg)
    ss.set_trusted_roles([2])(cfg)
    assert parse_config(cfg).trust.roles == {2: 3}


def test_no_escalation_for_groups() -> None:
    resolver = PermissionResolver(
        [Grant(c, "role", 1) for c in ss.GROUPS["moderators"]] + [Grant("perm.manage", "role", 1)], TrustRules()
    )
    mod_with_perm = Principal(user_id=5, role_ids=frozenset({1}))
    assert ss.can_edit_group(resolver, mod_with_perm, "moderators")
    assert not ss.can_edit_group(resolver, mod_with_perm, "admins")  # lacks config.manage etc.
    assert ss.can_edit_group(resolver, Principal(user_id=6, is_guild_admin=True), "admins")


def _answer(mode: str, style: str = "en", **kw: Any) -> Any:
    item = SimpleNamespace(
        claim_id=12,
        statement="Boss respawns every 4h.",
        verification="verified",
        basis="human",
        needs_review=False,
        groups=1,
        category_key="content",
        sources=[
            SimpleNamespace(
                kind="discord_message",
                title=None,
                evidence_at=datetime(2026, 9, 1, tzinfo=UTC),
                link="https://discord.com/channels/1/2/3",
            )
        ],
    )
    base = {"mode": mode, "style": style, "items": [item] if mode in ("extractive", "list") else [], "off_topic": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_render_extractive_has_status_sources_and_feedback() -> None:
    p = render_answer(_answer("extractive"), "AION 2")
    assert p.embed is not None and "Boss respawns" in p.embed.description
    names = [f.name for f in p.embed.fields]
    assert names == ["Status", "Sources"]
    assert "✅" in p.embed.fields[0].value and "discord.com/channels/1/2/3" in p.embed.fields[1].value
    assert p.embed.footer == "Record K-12 · content"
    assert [b.custom_id for b in p.buttons] == ["guru:fb:up", "guru:fb:down"]


def test_render_localization_greek_and_greeklish() -> None:
    el = render_answer(_answer("no_answer", "el", off_topic=True), "AION 2")
    gl = render_answer(_answer("no_answer", "greeklish", off_topic=True), "AION 2")
    assert el.embed and "Δεν έχω" in el.embed.description and "AION 2" in el.embed.description
    assert gl.embed and gl.embed.description.startswith("Den exw")
    assert tr("state.verified", "greeklish") == "Epivevaiwmeno"


def test_render_limited() -> None:
    p = render_limited("minute", 12.4, "el", delete_after=8)
    assert p.content and "12s" in p.content and p.delete_after == 8
