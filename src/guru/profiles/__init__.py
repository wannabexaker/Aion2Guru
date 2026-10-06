"""Bundled profile templates."""

from __future__ import annotations

from importlib import resources


def template_names() -> list[str]:
    root = resources.files("guru.profiles")
    return sorted(p.name.removesuffix(".yaml") for p in root.iterdir() if p.name.endswith(".yaml"))


def load_template(name: str) -> str:
    return (resources.files("guru.profiles") / f"{name}.yaml").read_text(encoding="utf-8")
