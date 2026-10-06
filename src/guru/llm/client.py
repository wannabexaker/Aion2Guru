"""LLM access: provider adapters (Ollama native, OpenAI-compatible), per-task model routing,
structured JSON output, circuit breaker and interactive-first priority. Provider-agnostic callers.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from guru.logging import get_logger
from guru.observability import metrics
from guru.settings import LLMProviderSettings, LLMTaskSettings

log = get_logger(__name__)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Provider down, timeout or circuit open → callers degrade (extractive answers, retry later)."""


class LLMBadOutput(LLMError):
    """Output not parseable/valid after the repair attempt."""


@dataclass(frozen=True)
class RawCompletion:
    text: str
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    task: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float

    @property
    def extractor_tag(self) -> str:
        return f"llm:{self.model}"


class Provider(Protocol):
    async def chat_json(
        self, task: LLMTaskSettings, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> RawCompletion: ...


class OllamaProvider:
    """Native /api/chat with `format` = JSON Schema (constrained decoding)."""

    def __init__(self, cfg: LLMProviderSettings, client: httpx.AsyncClient | None = None) -> None:
        self.base = cfg.base_url.rstrip("/")
        self.client = client or httpx.AsyncClient()

    async def chat_json(
        self, task: LLMTaskSettings, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> RawCompletion:
        options: dict[str, Any] = {"temperature": task.temperature, "num_predict": task.max_tokens}
        if task.seed is not None:
            options["seed"] = task.seed
        if task.num_ctx:
            options["num_ctx"] = task.num_ctx
        body: dict[str, Any] = {
            "model": task.model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "options": options,
        }
        if task.think is not None:
            body["think"] = task.think
        resp = await self.client.post(f"{self.base}/api/chat", json=body, timeout=task.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return RawCompletion(
            text=data.get("message", {}).get("content", ""),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )


class OpenAICompatProvider:
    """/v1/chat/completions with response_format json_schema (vLLM, llama.cpp server, Ollama /v1)."""

    def __init__(self, cfg: LLMProviderSettings, api_key: str | None, client: httpx.AsyncClient | None = None) -> None:
        self.base = cfg.base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.AsyncClient()

    async def chat_json(
        self, task: LLMTaskSettings, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> RawCompletion:
        body: dict[str, Any] = {
            "model": task.model,
            "messages": messages,
            "temperature": task.temperature,
            "max_tokens": task.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}},
        }
        if task.seed is not None:
            body["seed"] = task.seed
        url = f"{self.base}/chat/completions" if self.base.endswith("/v1") else f"{self.base}/v1/chat/completions"
        resp = await self.client.post(url, json=body, headers=self.headers, timeout=task.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        return RawCompletion(
            text=data["choices"][0]["message"].get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


class _Breaker:
    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.open_until = 0.0

    def check(self) -> None:
        if time.monotonic() < self.open_until:
            raise LLMUnavailable("circuit open")

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.cooldown_s
            log.warning("llm.circuit_open", cooldown_s=self.cooldown_s)


class _PriorityGate:
    """Background calls wait while interactive calls are in flight (same process)."""

    def __init__(self) -> None:
        self.interactive = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def enter(self, interactive: bool) -> None:
        if interactive:
            self.interactive += 1
            self.idle.clear()
        else:
            await self.idle.wait()

    def leave(self, interactive: bool) -> None:
        if interactive:
            self.interactive -= 1
            if self.interactive == 0:
                self.idle.set()


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")
    return data


class LLMClient:
    def __init__(self, providers: dict[str, Provider], tasks: dict[str, LLMTaskSettings]) -> None:
        self.providers = providers
        self.tasks = tasks
        self._breakers: dict[str, _Breaker] = {name: _Breaker() for name in providers}
        self._gate = _PriorityGate()

    def enabled(self, task: str) -> bool:
        cfg = self.tasks.get(task)
        return cfg is not None and cfg.provider in self.providers

    def model_of(self, task: str) -> str | None:
        cfg = self.tasks.get(task)
        return cfg.model if cfg else None

    async def run(
        self,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        interactive: bool = False,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> LLMResult:
        cfg = self.tasks.get(task)
        if cfg is None or cfg.provider not in self.providers:
            raise LLMUnavailable(f"task {task!r} not configured")
        provider = self.providers[cfg.provider]
        breaker = self._breakers[cfg.provider]
        breaker.check()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        await self._gate.enter(interactive)
        t0 = time.perf_counter()
        try:
            for attempt in (1, 2):
                try:
                    raw = await provider.chat_json(cfg, messages, schema)
                except (httpx.HTTPError, OSError) as exc:
                    breaker.failure()
                    raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
                breaker.success()
                metrics.LLM_TOKENS.labels(task=task, kind="prompt").inc(raw.prompt_tokens)
                metrics.LLM_TOKENS.labels(task=task, kind="completion").inc(raw.completion_tokens)
                try:
                    data = _parse_json(raw.text)
                    if validate is not None:
                        validate(data)
                    return LLMResult(
                        data,
                        task,
                        cfg.model,
                        raw.prompt_tokens,
                        raw.completion_tokens,
                        (time.perf_counter() - t0) * 1000,
                    )
                except (ValueError, TypeError) as exc:
                    if attempt == 2:
                        raise LLMBadOutput(str(exc)[:500]) from exc
                    messages = [
                        *messages,
                        {"role": "assistant", "content": raw.text[:4000]},
                        {
                            "role": "user",
                            "content": f"Invalid output ({str(exc)[:300]}). "
                            "Reply again with ONLY a JSON object that matches the schema.",
                        },
                    ]
            raise LLMBadOutput("unreachable")
        finally:
            self._gate.leave(interactive)


def build_llm_client(settings: Any) -> LLMClient:
    import os

    providers: dict[str, Provider] = {}
    for name, p in settings.llm_providers.items():
        if p.type == "ollama":
            providers[name] = OllamaProvider(p)
        else:
            providers[name] = OpenAICompatProvider(p, os.environ.get(p.api_key_env) if p.api_key_env else None)
    return LLMClient(providers, dict(settings.llm_tasks))


# ---------------------------------------------------------------- test double


class FakeProvider:
    """Deterministic provider for tests: a function per task returns the JSON object."""

    def __init__(
        self, handlers: dict[str, Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]] | dict[str, Any]]]
    ):
        self.handlers = handlers
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def chat_json(
        self, task: LLMTaskSettings, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> RawCompletion:
        key = task.model  # tests use the task name as the model name
        self.calls.append((key, messages))
        out = self.handlers[key](messages)
        if asyncio.iscoroutine(out):
            out = await out
        return RawCompletion(json.dumps(out), 100, 20)


def fake_client(handlers: dict[str, Any]) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(handlers)
    tasks = {t: LLMTaskSettings(provider="fake", model=t) for t in handlers}
    return LLMClient({"fake": provider}, tasks), provider
