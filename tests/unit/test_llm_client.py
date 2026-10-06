from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from guru.llm.client import (
    LLMBadOutput,
    LLMClient,
    LLMUnavailable,
    OllamaProvider,
    OpenAICompatProvider,
)
from guru.llm.embeddings import OllamaEmbedder
from guru.settings import EmbeddingSettings, LLMProviderSettings, LLMTaskSettings

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ollama_request_shape_and_parse() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        assert req.url.path == "/api/chat"
        return httpx.Response(
            200, json={"message": {"content": '{"ok": true}'}, "prompt_eval_count": 12, "eval_count": 3}
        )

    provider = OllamaProvider(LLMProviderSettings(base_url="http://ollama:11434"), _client(handler))
    llm = LLMClient({"local": provider}, {"extract": LLMTaskSettings(model="gpt-oss:20b", think="low")})
    res = await llm.run("extract", "sys", "user", SCHEMA)
    assert res.data == {"ok": True} and res.prompt_tokens == 12 and res.model == "gpt-oss:20b"
    assert seen["format"] == SCHEMA and seen["stream"] is False and seen["think"] == "low"
    assert seen["options"]["temperature"] == 0.0 and seen["options"]["seed"] == 42
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]


async def test_openai_compat_request_shape() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer k"
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": false}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatProvider(
        LLMProviderSettings(type="openai_compat", base_url="http://vllm:8000"), "k", _client(handler)
    )
    llm = LLMClient({"v": provider}, {"answer": LLMTaskSettings(provider="v", model="m")})
    assert (await llm.run("answer", "s", "u", SCHEMA)).data == {"ok": False}


async def test_repair_retry_then_bad_output() -> None:
    replies = iter(["not json", '{"ok": true}', "still bad", "nope"])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": next(replies)}})

    llm = LLMClient(
        {"local": OllamaProvider(LLMProviderSettings(), _client(handler))}, {"t": LLMTaskSettings(model="m")}
    )
    assert (await llm.run("t", "s", "u", SCHEMA)).data == {"ok": True}  # repaired on 2nd attempt
    with pytest.raises(LLMBadOutput):
        await llm.run("t", "s", "u", SCHEMA)


async def test_circuit_breaker_opens_after_failures() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused")

    llm = LLMClient(
        {"local": OllamaProvider(LLMProviderSettings(), _client(handler))}, {"t": LLMTaskSettings(model="m")}
    )
    for _ in range(3):
        with pytest.raises(LLMUnavailable):
            await llm.run("t", "s", "u", SCHEMA)
    with pytest.raises(LLMUnavailable, match="circuit open"):
        await llm.run("t", "s", "u", SCHEMA)
    assert calls == 3  # the 4th call never hit the network


async def test_background_waits_for_interactive() -> None:
    order: list[str] = []
    release = asyncio.Event()

    async def slow(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        tag = body["messages"][1]["content"]
        if tag == "interactive":
            await release.wait()
        order.append(tag)
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})

    llm = LLMClient({"local": OllamaProvider(LLMProviderSettings(), _client(slow))}, {"t": LLMTaskSettings(model="m")})
    interactive = asyncio.create_task(llm.run("t", "s", "interactive", SCHEMA, interactive=True))
    await asyncio.sleep(0.05)
    background = asyncio.create_task(llm.run("t", "s", "background", SCHEMA))
    await asyncio.sleep(0.05)
    assert order == []  # background is held back
    release.set()
    await asyncio.gather(interactive, background)
    assert order == ["interactive", "background"]


async def test_unconfigured_task_is_unavailable() -> None:
    llm = LLMClient({}, {})
    assert not llm.enabled("extract")
    with pytest.raises(LLMUnavailable):
        await llm.run("extract", "s", "u", SCHEMA)


async def test_ollama_embedder_batches_and_normalizes() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.url.path == "/api/embed" and body["model"] == "bge-m3"
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0] for _ in body["input"]]})

    emb = OllamaEmbedder(EmbeddingSettings(provider="ollama", model="bge-m3", dims=2, batch_size=2), _client(handler))
    vectors = await emb.embed(["a", "b", "c"])
    assert len(vectors) == 3 and vectors[0] == pytest.approx([0.6, 0.8])
