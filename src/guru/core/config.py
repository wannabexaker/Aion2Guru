"""Profile configuration schema (v1). Validated with Pydantic + semantic checks.

Everything profile-specific lives here; the core code has no game-specific logic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

import re2
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from guru.core.permissions import ALL_CAPABILITIES, GroupName

SCHEMA_VERSION = 1


def _to_int(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return v


Snowflake = Annotated[int, BeforeValidator(_to_int)]
Key = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")]
Tier = Annotated[int, Field(ge=0, le=4)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_re2(patterns: list[str]) -> list[str]:
    for p in patterns:
        try:
            re2.compile(p)
        except Exception as exc:  # re2.error
            raise ValueError(f"invalid RE2 pattern {p!r}: {exc}") from exc
    return patterns


# ---------------------------------------------------------------- profile


class LanguagesCfg(_Base):
    canonical: Literal["en", "el"] = "en"
    accepted: list[Literal["en", "el"]] = ["en", "el"]
    answer: Literal["match_question", "canonical"] = "match_question"
    greeklish: bool = True


class ProfileMeta(_Base):
    slug: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,40}$")]
    name: str
    guild_id: Snowflake | None = None
    description: str = ""
    languages: LanguagesCfg = LanguagesCfg()
    keywords: list[str] = []


# ---------------------------------------------------------------- applicability dimensions


class DimensionValue(_Base):
    value: str
    valid_from: date | None = None
    scope: dict[str, str] = {}


class Dimension(_Base):
    type: Literal["ordered", "enum"]
    infer_from_date: bool = False
    patterns: list[str] = []
    values: list[DimensionValue] = []
    default: list[str] = []

    @field_validator("values", mode="before")
    @classmethod
    def _coerce_values(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [{"value": x} if isinstance(x, str) else x for x in v]
        return v

    @field_validator("patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        return _check_re2(v)


# ---------------------------------------------------------------- channels


class IngestPolicy(_Base):
    mode: Literal["all", "hybrid", "explicit_only", "off"] = "hybrid"
    min_author_tier: Tier = 2
    include_threads: bool = True
    signal_bonus: int = 0


ChannelRole = Literal["home", "watch", "ask", "faq_publish", "faq_review", "mod_review", "admin_log"]


class ChannelBinding(_Base):
    channel_id: Snowflake
    role: ChannelRole
    default_category: str | None = None
    audience: Literal["public", "restricted"] = "public"
    ingest: IngestPolicy = IngestPolicy()


# ---------------------------------------------------------------- categories & structure


class CategorySettings(_Base):
    half_life_days: float | None = 90
    volatile_on_version: bool = False
    faq_eligible: bool = False


class Category(_Base):
    key: Key
    name: str
    description: str = ""
    keywords: list[str] = []
    aliases: list[str] = []
    settings: CategorySettings = CategorySettings()
    children: list[Category] = []


class AttributeDef(_Base):
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    label: dict[str, str] = {}
    answer_template: dict[str, str] = {}


class EntityType(_Base):
    key: Key
    name: str
    default_category: str | None = None
    attributes: dict[str, AttributeDef] = {}


class Intent(_Base):
    key: Key
    description: str = ""
    target: dict[str, Any]
    patterns: list[str] = []
    examples: list[str] = []

    @field_validator("patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        return _check_re2(v)


# ---------------------------------------------------------------- sources & trust


class Source(_Base):
    key: Key
    kind: Literal["web_page", "web_feed", "web_sitemap", "web_search"]
    name: str | None = None
    locator: str
    independence_group: str
    trust_tier: Tier
    trust_pinned: bool = False
    schedule_seconds: int | None = Field(default=None, ge=300)
    fetch_config: dict[str, Any] = {}


class DomainTrust(_Base):
    pattern: str
    tier: Tier
    group: str | None = None


class CorroborationRule(_Base):
    enabled: bool = True
    min_groups: int = Field(default=2, ge=1)
    min_tier: Tier = 2


class VerificationPolicy(_Base):
    auto_verify_official: bool = True
    # Explicit captures (/kb add, 📌, "Add to knowledge") by members at or above this tier count as
    # human verification. The team's own statements are the reference (D-11).
    auto_verify_trusted_explicit: bool = True
    trusted_explicit_min_tier: Tier = 3
    auto_verify_corroboration: CorroborationRule = CorroborationRule(enabled=False, min_groups=3, min_tier=3)
    corroborated: CorroborationRule = CorroborationRule()


class TrustCfg(_Base):
    tier_weights: dict[int, float] = {0: 0.0, 1: 0.25, 2: 0.5, 3: 0.8, 4: 1.0}
    default_member_tier: Tier = 2
    new_account_days: int = Field(default=14, ge=0)
    roles: dict[Snowflake, Tier] = {}
    users: dict[Snowflake, Tier] = {}
    webhooks: dict[Snowflake, Tier] = {}
    community_cap: float = Field(default=1.2, gt=0)
    freshness_floor: float = Field(default=0.2, ge=0, le=1)
    version_mismatch_factor: float = Field(default=0.4, ge=0, le=1)
    unverified_quote_factor: float = Field(default=0.5, ge=0, le=1)
    verification_policy: VerificationPolicy = VerificationPolicy()

    @field_validator("tier_weights")
    @classmethod
    def _weights(cls, v: dict[int, float]) -> dict[int, float]:
        if set(v) != {0, 1, 2, 3, 4}:
            raise ValueError("tier_weights must define tiers 0..4")
        if any(not 0 <= w <= 1 for w in v.values()) or any(v[i] > v[i + 1] for i in range(4)):
            raise ValueError("tier_weights must be in [0,1] and non-decreasing")
        return v


class PermissionCfg(_Base):
    capability: str
    roles: list[Snowflake] = []
    users: list[Snowflake] = []
    everyone: bool = False

    @field_validator("capability")
    @classmethod
    def _cap(cls, v: str) -> str:
        if v not in ALL_CAPABILITIES:
            raise ValueError(f"unknown capability {v!r}")
        return v


# ---------------------------------------------------------------- access, limits, review


class AccessCfg(_Base):
    """D-26: what happens when someone without kb.query talks to the bot."""

    unauthorized_action: Literal["delete", "ignore", "notice"] = "delete"
    notice_seconds: int = Field(default=8, ge=0, le=120)
    enforce_in_home: bool = True
    enforce_on_mention: bool = True
    bootstrap_admins: bool = True


class RateLimitRule(_Base):
    per_minute: int | None = Field(default=3, ge=0)
    per_hour: int | None = Field(default=20, ge=0)
    per_day: int | None = Field(default=60, ge=0)
    llm_per_day: int | None = Field(default=30, ge=0)


class RateLimitCfg(_Base):
    default: RateLimitRule = RateLimitRule()
    roles: dict[Snowflake, RateLimitRule] = {}
    exempt_roles: list[Snowflake] = []


class ReviewAuto(_Base):
    enabled: bool = False
    threshold: float = Field(default=0.95, gt=0.5, le=1)
    min_labels: int = Field(default=200, ge=20)


class ClaimKeepReview(_Base):
    enabled: bool = True
    auto: ReviewAuto = ReviewAuto()


class AnswerRatingReview(_Base):
    enabled: bool = True
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    only_llm: bool = False


class ReviewCfg(_Base):
    """D-27: moderator review channel and learned automation."""

    quorum: int = Field(default=1, ge=1, le=10)
    claim_keep: ClaimKeepReview = ClaimKeepReview()
    answer_rating: AnswerRatingReview = AnswerRatingReview()


# ---------------------------------------------------------------- ingestion, search, answer


class PrefilterCfg(_Base):
    min_chars: int = 25
    max_chars: int = 4000
    threshold: int = 2
    weights: dict[str, int] = {
        "entity_alias_hit": 2,
        "keyword_hit": 1,
        "number_or_unit": 1,
        "allowlisted_url": 1,
        "reply_to_question": 1,
        "author_tier_ge_3": 2,
    }


class WindowCfg(_Base):
    idle_seconds: int = 300
    max_messages: int = 30
    max_tokens: int = 1500
    context_before: int = 5


class CaptureCfg(_Base):
    reaction_emoji: str = "📌"
    reaction_min_tier: Tier = 3


class IngestionCfg(_Base):
    prefilter: PrefilterCfg = PrefilterCfg()
    window: WindowCfg = WindowCfg()
    capture: CaptureCfg = CaptureCfg()
    allowed_claim_types: list[Literal["fact", "tip", "procedure"]] = ["fact", "tip", "procedure"]
    quote_min_ratio: float = Field(default=0.9, gt=0, le=1)


class RelevanceCfg(_Base):
    accept: float = 0.55
    reject: float = 0.35


class DedupeCfg(_Base):
    tau_high: float = 0.92
    tau_low: float = 0.80


class SearchCfg(_Base):
    default_scope: Literal["faq", "verified", "internal", "web", "all"] = "all"
    default_min_state: Literal["unverified", "corroborated", "verified"] = "unverified"
    relevance: RelevanceCfg = RelevanceCfg()
    category_filter: Literal["soft", "hard"] = "soft"
    hybrid: Literal["fallback", "always"] = "fallback"
    fts_k: int = 20
    vector_k: int = 20
    rrf_k: int = 60
    state_factors: dict[str, float] = {
        "verified": 1.0,
        "corroborated": 0.85,
        "disputed": 0.75,
        "unverified": 0.6,
    }
    dedupe: DedupeCfg = DedupeCfg()
    faq_match_threshold: float = 0.85
    min_coverage: float = Field(default=0.5, gt=0, le=1)
    trigram_threshold: float = Field(default=0.45, gt=0, le=1)


class AnswerCfg(_Base):
    llm_synthesis: Literal["auto", "always", "never"] = "auto"
    allow_cross_language_extractive: bool = True
    max_context_records: int = Field(default=6, ge=1, le=12)
    max_context_tokens: int = 1800
    max_answer_tokens: int = 350
    dominance_ratio: float = 1.6
    max_records_listed: int = Field(default=3, ge=1, le=5)
    show_record_ids: bool = True


class FaqCandidatesCfg(_Base):
    on_verified: bool = True
    popularity: dict[str, int] = {"min_queries": 5, "window_days": 14}


class FaqCfg(_Base):
    format: Literal["forum", "text"] = "forum"
    tags_from: Literal["top_level_categories"] = "top_level_categories"
    candidates: FaqCandidatesCfg = FaqCandidatesCfg()
    require_four_eyes: bool = False
    auto_publish: dict[str, Any] = {"enabled": False}
    on_deprecate: Literal["mark", "delete"] = "mark"
    on_disputed: Literal["banner", "deprecate", "none"] = "banner"


class PromptsCfg(_Base):
    persona: str = ""
    answer_style: str = ""
    extraction_guidelines: str = ""
    faq_style: str = ""


class RetentionCfg(_Base):
    context_only_days: int = 14
    unlinked_candidate_days: int = 60
    query_log_days: int = 30
    on_source_delete: Literal["keep_knowledge", "purge"] = "keep_knowledge"


DEFAULT_SCOPE_TOKENS: dict[str, list[str]] = {
    "faq": ["faq"],
    "verified": ["verified"],
    "internal": ["internal", "kb"],
    "web": ["web", "internet"],
    "all": ["all"],
}


# ---------------------------------------------------------------- root


class ProfileConfig(_Base):
    schema_version: Literal[1] = 1
    profile: ProfileMeta
    dimensions: dict[str, Dimension] = {}
    channels: list[ChannelBinding] = []
    categories: list[Category]
    entity_types: list[EntityType] = []
    intents: list[Intent] = []
    sources: list[Source] = []
    domain_trust: list[DomainTrust] = []
    domain_deny: list[str] = []
    trust: TrustCfg = TrustCfg()
    permissions: list[PermissionCfg] = []
    # /settings role groups (ai_users, contributors, moderators, admins) → role ids.
    role_groups: dict[GroupName, list[Snowflake]] = {}
    access: AccessCfg = AccessCfg()
    rate_limits: RateLimitCfg = RateLimitCfg()
    review: ReviewCfg = ReviewCfg()
    ingestion: IngestionCfg = IngestionCfg()
    search: SearchCfg = SearchCfg()
    answer: AnswerCfg = AnswerCfg()
    scope_tokens: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_SCOPE_TOKENS))

    @field_validator("scope_tokens")
    @classmethod
    def _scopes(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = set(v) - set(DEFAULT_SCOPE_TOKENS)
        if unknown:
            raise ValueError(f"unknown scopes {sorted(unknown)}")
        return v

    faq: FaqCfg = FaqCfg()
    prompts: PromptsCfg = PromptsCfg()
    retention: RetentionCfg = RetentionCfg()

    # ------------------------------------------------------------ helpers
    def flat_categories(self) -> list[tuple[Category, str | None]]:
        out: list[tuple[Category, str | None]] = []

        def walk(items: list[Category], parent: str | None) -> None:
            for c in items:
                out.append((c, parent))
                walk(c.children, c.key)

        walk(self.categories, None)
        return out

    def category_keys(self) -> set[str]:
        return {c.key for c, _ in self.flat_categories()}

    def channels_with_role(self, role: ChannelRole) -> list[ChannelBinding]:
        return [c for c in self.channels if c.role == role]

    # ------------------------------------------------------------ semantic validation
    @model_validator(mode="after")
    def _semantics(self) -> ProfileConfig:
        errors: list[str] = []
        keys = [c.key for c, _ in self.flat_categories()]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            errors.append(f"duplicate category keys: {dup}")
        if not keys:
            errors.append("at least one category is required")
        cats = set(keys)
        for ch in self.channels:
            if ch.default_category and ch.default_category not in cats:
                errors.append(f"channel {ch.channel_id}: unknown default_category {ch.default_category!r}")
        seen: set[tuple[int, str]] = set()
        for ch in self.channels:
            if (ch.channel_id, ch.role) in seen:
                errors.append(f"channel {ch.channel_id} bound twice with role {ch.role}")
            seen.add((ch.channel_id, ch.role))
        answering = [c.channel_id for c in self.channels if c.role in ("home", "ask")]
        if len(answering) != len(set(answering)):
            errors.append("a channel cannot be both 'home' and 'ask'")
        if len(self.channels_with_role("mod_review")) > 1:
            errors.append("at most one mod_review channel")
        et_keys = [e.key for e in self.entity_types]
        if len(et_keys) != len(set(et_keys)):
            errors.append("duplicate entity_type keys")
        for et in self.entity_types:
            if et.default_category and et.default_category not in cats:
                errors.append(f"entity_type {et.key}: unknown default_category {et.default_category!r}")
        it_keys = [i.key for i in self.intents]
        if len(it_keys) != len(set(it_keys)):
            errors.append("duplicate intent keys")
        for it in self.intents:
            cat = it.target.get("category")
            if cat and cat not in cats:
                errors.append(f"intent {it.key}: unknown target category {cat!r}")
            target_type = it.target.get("entity_type")
            if target_type and target_type not in et_keys:
                errors.append(f"intent {it.key}: unknown target entity_type {target_type!r}")
        src_keys = [s.key for s in self.sources]
        if len(src_keys) != len(set(src_keys)):
            errors.append("duplicate source keys")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    # ------------------------------------------------------------ serialization
    def to_jsonable(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def config_hash(self) -> bytes:
        canonical = json.dumps(self.to_jsonable(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).digest()


class ConfigError(ValueError):
    """Human-readable validation failure."""


def parse_config(data: dict[str, Any]) -> ProfileConfig:
    try:
        return ProfileConfig.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"{loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def load_yaml(text: str) -> ProfileConfig:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError("config must be a YAML mapping")
    return parse_config(data)


def dump_yaml(cfg: ProfileConfig) -> str:
    return yaml.safe_dump(cfg.to_jsonable(), sort_keys=False, allow_unicode=True, width=110)


def diff_configs(old: dict[str, Any] | None, new: dict[str, Any], prefix: str = "") -> list[str]:
    """Deterministic, path-level diff for previews and audit."""
    if old is None:
        return [f"+ {prefix or '<config>'} (new)"]
    out: list[str] = []
    for key in sorted(set(old) | set(new), key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in old:
            out.append(f"+ {path} = {_short(new[key])}")
        elif key not in new:
            out.append(f"- {path}")
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            out.extend(diff_configs(old[key], new[key], path))
        elif old[key] != new[key]:
            out.append(f"~ {path}: {_short(old[key])} → {_short(new[key])}")
    return out


def _short(v: Any, limit: int = 80) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"
