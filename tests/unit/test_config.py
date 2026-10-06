from __future__ import annotations

import pytest

from guru.core.config import ConfigError, diff_configs, dump_yaml, load_yaml, parse_config
from guru.profiles import load_template, template_names


def _minimal(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "profile": {"slug": "test", "name": "Test"},
        "categories": [{"key": "general", "name": "General"}],
    }
    base.update(extra)
    return base


def test_bundled_templates_validate_and_roundtrip() -> None:
    assert "aion2" in template_names()
    cfg = load_yaml(load_template("aion2"))
    assert cfg.profile.slug == "aion2"
    assert {"general", "classes", "skills", "items"} <= cfg.category_keys()
    again = load_yaml(dump_yaml(cfg))
    assert again.config_hash() == cfg.config_hash()


def test_snowflakes_accept_strings() -> None:
    cfg = parse_config(_minimal(channels=[{"channel_id": "1234567890123456789", "role": "home"}]))
    assert cfg.channels[0].channel_id == 1234567890123456789


def test_extra_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="Extra inputs"):
        parse_config(_minimal(unknown_section={}))


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"categories": [{"key": "a", "name": "A"}, {"key": "a", "name": "B"}]}, "duplicate category"),
        ({"channels": [{"channel_id": 1, "role": "home", "default_category": "nope"}]}, "unknown default_category"),
        ({"channels": [{"channel_id": 1, "role": "home"}, {"channel_id": 1, "role": "ask"}]}, "both 'home' and 'ask'"),
        ({"intents": [{"key": "x", "target": {}, "patterns": ["(unclosed"]}]}, "invalid RE2"),
        ({"intents": [{"key": "x", "target": {"category": "zzz"}}]}, "unknown target category"),
        ({"permissions": [{"capability": "kb.everything"}]}, "unknown capability"),
        ({"trust": {"tier_weights": {0: 0, 1: 0.9, 2: 0.5, 3: 0.8, 4: 1}}}, "non-decreasing"),
    ],
)
def test_semantic_validation(patch: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(_minimal(**patch))


def test_re2_rejects_backreferences() -> None:
    # RE2 has no backtracking constructs → admin regex cannot cause ReDoS.
    with pytest.raises(ConfigError, match="invalid RE2"):
        parse_config(_minimal(intents=[{"key": "x", "target": {}, "patterns": [r"(a)\1"]}]))


def test_diff_is_path_level() -> None:
    old = {"access": {"unauthorized_action": "delete", "notice_seconds": 8}, "x": 1}
    new = {"access": {"unauthorized_action": "notice", "notice_seconds": 8}, "y": 2}
    assert diff_configs(old, new) == [
        '~ access.unauthorized_action: "delete" → "notice"',
        "- x",
        "+ y = 2",
    ]
