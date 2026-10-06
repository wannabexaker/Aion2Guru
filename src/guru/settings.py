"""Global (non-profile) settings: secrets from env, the rest from guru.yaml.

Profile-level configuration lives in the database (versioned) — see guru.core.config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class LLMProviderSettings(BaseModel):
    type: Literal["ollama", "openai_compat"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    api_key_env: str | None = None


class LLMTaskSettings(BaseModel):
    provider: str = "local"
    model: str
    temperature: float = 0.0
    max_tokens: int = 512
    timeout_s: float = 60.0
    seed: int | None = 42
    # Ollama reasoning control: None = model default, False = off, "low"/"medium"/"high" for gpt-oss.
    think: bool | Literal["low", "medium", "high"] | None = None
    num_ctx: int | None = 8192


class EmbeddingSettings(BaseModel):
    provider: Literal["ollama", "none"] = "none"
    base_url: str = "http://127.0.0.1:11434"
    model: str = ""
    dims: int = 0
    # Some models (e5) need role prefixes; bge-m3 does not.
    query_prefix: str = ""
    document_prefix: str = ""
    batch_size: int = 32
    timeout_s: float = 60.0


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class WorkerSettings(BaseModel):
    concurrency: int = 2
    lease_seconds: int = 300
    poll_seconds: float = 5.0


def _config_file() -> Path:
    return Path(os.environ.get("GURU_CONFIG", "guru.yaml"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GURU_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    database_url: SecretStr = SecretStr("postgresql://guru@localhost:5432/guru")
    discord_token: SecretStr | None = None
    # Salt for hashing user ids in logs/query_log. Must be set in production.
    hash_salt: SecretStr = SecretStr("change-me")
    # Discord user ids with full access for initial setup (break-glass).
    owner_ids: list[int] = Field(default_factory=list)
    # Restrict app-command sync to these guilds (fast propagation). Empty = global.
    guild_ids: list[int] = Field(default_factory=list)

    log_level: str = "INFO"
    log_json: bool = True

    api: ApiSettings = Field(default_factory=ApiSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    llm_providers: dict[str, LLMProviderSettings] = Field(default_factory=lambda: {"local": LLMProviderSettings()})
    llm_tasks: dict[str, LLMTaskSettings] = Field(default_factory=dict)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit init > env > secret files > guru.yaml
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, file_secret_settings]
        path = _config_file()
        if path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=path))
        return tuple(sources)


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
