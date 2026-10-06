"""Structured JSON logging with correlation ids carried in contextvars."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    # discord.py / uvicorn use stdlib logging; keep them quiet and on stdout.
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation(cid: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a unit of work (query, job)."""
    value = cid or uuid.uuid4().hex
    token = _correlation_id.set(value)
    with structlog.contextvars.bound_contextvars(correlation_id=value):
        try:
            yield value
        finally:
            _correlation_id.reset(token)
