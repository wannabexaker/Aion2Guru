"""Builds every /settings view offline to catch discord.py API misuse (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from guru.core.aliases import AliasMatcher
from guru.core.config import load_yaml
from guru.core.permissions import PermissionResolver, Principal, TrustRules
from guru.discord_bot import settings_view as sv
from guru.profiles import load_template
from guru.services import settings_service as ss
from guru.services.profiles import ProfileState


def _state() -> ProfileState:
    data = load_yaml(load_template("aion2")).to_jsonable()
    data["profile"]["guild_id"] = 1
    ss.set_group_roles("moderators", [10])(data)
    ss.set_channel("home", [20])(data)
    ss.set_channel("mod_review", [21])(data)
    cfg = load_yaml(__import__("yaml").safe_dump(data))
    return ProfileState(
        profile_id=1,
        slug="aion2",
        guild_id=1,
        version=3,
        knowledge_epoch=0,
        config=cfg,
        category_ids={},
        category_keys={},
        source_ids={},
        matcher=AliasMatcher(),
        resolver=PermissionResolver([], TrustRules()),
        channel_roles={20: {"home"}, 21: {"mod_review"}},
    )


async def test_all_settings_views_construct() -> None:
    state = _state()
    bot = SimpleNamespace(registry=SimpleNamespace(by_slug=lambda slug: state))
    admin = Principal(user_id=1, is_guild_admin=True)
    view = sv.SettingsView(bot, "aion2", admin)  # type: ignore[arg-type]
    embed = view.summary_embed()
    assert "<@&10>" in embed.fields[2].value and "<#20>" in embed.fields[5].value
    for sub in (
        sv.RoleGroupView(view, "moderators"),
        sv.TrustedView(view),
        sv.ChannelRoleView(view, "home"),
        sv.ChannelRoleView(view, "mod_review"),
        sv.AccessView(view),
    ):
        payload = sub.to_components()
        assert payload and len(payload) <= 5
    modal = sv.LimitsModal(view)
    assert len(modal.children) == 4
    sel = sv.SectionSelect(view)
    assert {o.value for o in sel.options} >= {"ai_users", "home", "limits"}
