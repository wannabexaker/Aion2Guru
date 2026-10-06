"""Embedding providers + storage. Raw Discord messages are never embedded — only canonical claims,
web chunks and prototypes (DESIGN X10)."""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

import asyncpg
import httpx
import numpy as np

from guru.settings import EmbeddingSettings


class Embedder(Protocol):
    name: str
    dims: int

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(self, cfg: EmbeddingSettings, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.name = f"ollama:{cfg.model}"
        self.dims = cfg.dims
        self.base = cfg.base_url.rstrip("/")
        self.client = client or httpx.AsyncClient()

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        prefix = self.cfg.query_prefix if query else self.cfg.document_prefix
        out: list[list[float]] = []
        for i in range(0, len(texts), self.cfg.batch_size):
            batch = [prefix + t for t in texts[i : i + self.cfg.batch_size]]
            resp = await self.client.post(
                f"{self.base}/api/embed", json={"model": self.cfg.model, "input": batch}, timeout=self.cfg.timeout_s
            )
            resp.raise_for_status()
            vectors = resp.json()["embeddings"]
            for v in vectors:
                if len(v) != self.dims:
                    raise ValueError(f"embedding dims {len(v)} != configured {self.dims}")
            out.extend(_normalize(v) for v in vectors)
        return out


class HashEmbedder:
    """Deterministic bag-of-tokens embedder for tests and dev without a model server."""

    def __init__(self, dims: int = 64) -> None:
        self.name = f"hash:{dims}"
        self.dims = dims

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        from guru.core.text import content_tokens

        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for tok in content_tokens(text):
                stem = tok[:5]
                h = int.from_bytes(hashlib.sha256(stem.encode()).digest()[:4], "big")
                vec[h % self.dims] += 1.0
            out.append(_normalize(vec))
        return out


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return float(np.dot(np.asarray(a), np.asarray(b)))


def build_embedder(cfg: EmbeddingSettings) -> Embedder | None:
    if cfg.provider == "ollama" and cfg.model and cfg.dims:
        return OllamaEmbedder(cfg)
    return None


# ---------------------------------------------------------------- storage


async def model_id(conn: asyncpg.Connection, embedder: Embedder) -> int:
    """Register the embedder; the newest registered model becomes the active one."""
    row = await conn.fetchrow("SELECT id, dims, status FROM embedding_models WHERE name = $1", embedder.name)
    if row is not None:
        if row["dims"] != embedder.dims:
            raise ValueError(f"embedding model {embedder.name} registered with {row['dims']} dims")
        if row["status"] != "active":
            await conn.execute("UPDATE embedding_models SET status = 'retired' WHERE status = 'active'")
            await conn.execute("UPDATE embedding_models SET status = 'active' WHERE id = $1", row["id"])
        return int(row["id"])
    await conn.execute("UPDATE embedding_models SET status = 'retired' WHERE status = 'active'")
    return int(
        await conn.fetchval(
            "INSERT INTO embedding_models (name, dims, status) VALUES ($1, $2, 'active') RETURNING id",
            embedder.name,
            embedder.dims,
        )
    )


def text_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


async def store(
    conn: asyncpg.Connection,
    *,
    owner_type: str,
    owner_id: int,
    model: int,
    profile_id: int,
    text: str,
    vector: list[float],
    ordinal: int = 0,
) -> None:
    await conn.execute(
        """INSERT INTO embeddings (owner_type, owner_id, ordinal, model_id, profile_id, content_hash, embedding)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (owner_type, owner_id, ordinal, model_id)
           DO UPDATE SET content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, created_at = now()""",
        owner_type,
        owner_id,
        ordinal,
        model,
        profile_id,
        text_hash(text),
        np.asarray(vector, dtype=np.float32),
    )


async def nearest_claims(
    conn: asyncpg.Connection,
    *,
    model: int,
    profile_id: int,
    vector: list[float],
    k: int = 5,
    exclude: int | None = None,
) -> list[tuple[int, float]]:
    """Exact cosine search among active claims (fine at MVP scale; HNSW when needed)."""
    rows = await conn.fetch(
        """SELECT e.owner_id, 1 - (e.embedding <=> $3) AS sim
             FROM embeddings e JOIN claims c ON c.id = e.owner_id
            WHERE e.owner_type = 'claim' AND e.model_id = $1 AND e.profile_id = $2
              AND c.lifecycle = 'active' AND ($5::bigint IS NULL OR c.id <> $5)
            ORDER BY e.embedding <=> $3
            LIMIT $4""",
        model,
        profile_id,
        np.asarray(vector, dtype=np.float32),
        k,
        exclude,
    )
    return [(int(r["owner_id"]), float(r["sim"])) for r in rows]
