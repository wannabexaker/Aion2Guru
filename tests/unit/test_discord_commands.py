"""Smoke test: the command tree builds and every command serializes to a valid Discord payload."""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from guru.discord_bot.client import GuruBot
from guru.discord_bot.commands import register_commands
from guru.settings import Settings


def test_command_tree_serializes() -> None:
    settings = Settings(hash_salt=SecretStr("x"))
    rt = SimpleNamespace(db=None, settings=settings, ready={}, dsn="")
    bot = GuruBot(rt)  # type: ignore[arg-type]
    register_commands(bot)
    payloads = {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}
    assert set(payloads) == {"ask", "kb", "setup", "settings", "admin", "Add to knowledge"}
    assert payloads["Add to knowledge"]["type"] == 3  # message context menu
    kb_subs = {o["name"] for o in payloads["kb"]["options"]}
    assert kb_subs == {"add", "show", "verify", "retract", "obsolete", "ingest-url"}
    ask_opts = {o["name"]: o for o in payloads["ask"]["options"]}
    assert ask_opts["question"]["required"] and ask_opts["category"]["autocomplete"]
    for p in payloads.values():
        assert len(p.get("description", "")) <= 100
