"""Thin internal API: health, readiness, metrics. Bind to localhost only."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from guru.db import Database


def create_app(db: Database, readiness: Callable[[], dict[str, bool]] | None = None) -> FastAPI:
    app = FastAPI(title="guru", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        checks: dict[str, bool] = {}
        try:
            checks["db"] = (await db.fetchval("SELECT 1")) == 1
        except Exception:
            checks["db"] = False
        if readiness is not None:
            checks.update(readiness())
        ok = all(checks.values())
        response.status_code = 200 if ok else 503
        return {"ready": ok, "checks": checks}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
