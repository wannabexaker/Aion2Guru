"""Applicability dimensions (DESIGN §5.7): map dates to versions deterministically. Pure."""

from __future__ import annotations

from datetime import date, datetime

from guru.core.config import Dimension, ProfileConfig


def value_at(dim: Dimension, at: date | datetime, scope: dict[str, str] | None = None) -> str | None:
    """Latest dimension value released on or before `at` (optionally restricted to a scope, e.g. region)."""
    day = at.date() if isinstance(at, datetime) else at
    best: tuple[date, int, str] | None = None
    for idx, v in enumerate(dim.values):
        if v.valid_from is None or v.valid_from > day:
            continue
        if scope and any(v.scope.get(k) not in (None, val) for k, val in scope.items()):
            continue
        key = (v.valid_from, idx, v.value)
        if best is None or key[:2] > best[:2]:
            best = key
    return None if best is None else best[2]


def default_scope(cfg: ProfileConfig) -> dict[str, str]:
    region = cfg.dimensions.get("region")
    return {"region": region.default[0]} if region is not None and region.default else {}


def version_at(cfg: ProfileConfig, at: date | datetime) -> str | None:
    dim = cfg.dimensions.get("game_version")
    return None if dim is None else value_at(dim, at, default_scope(cfg))
