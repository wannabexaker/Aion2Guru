"""Composition root: builds services and job handlers for the running roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from guru.jobs.worker import Handler

if TYPE_CHECKING:
    from guru.app import Runtime


def build_job_handlers(rt: Runtime) -> dict[str, Handler]:
    handlers: dict[str, Handler] = {}
    return handlers
