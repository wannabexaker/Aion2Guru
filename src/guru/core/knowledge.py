"""Evidence scoring and the verification state machine (DESIGN §5.4). Pure functions, no I/O.

Truth is derived from evidence — provenance, trust tier, independence, freshness, version — never
from a model's self-reported confidence. `derive()` is the single source of every derived field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from guru.core.config import ProfileConfig

Stance = Literal["supports", "contradicts"]
Verification = Literal["unverified", "corroborated", "verified", "disputed"]
Basis = Literal["human", "official_source", "corroboration"]

STATE_ORDER: dict[str, int] = {"unverified": 0, "disputed": 0, "corroborated": 1, "verified": 2}


@dataclass(frozen=True)
class EvidenceFacts:
    stance: Stance
    tier: int
    group: str
    origin: str
    evidence_at: datetime
    quote_verified: bool = True
    human: bool = False  # entered/selected by a human, not machine-extracted
    endorser_tier: int | None = None  # tier of the human who explicitly captured it
    version_match: bool | None = None  # None = unknown
    audience_channel_id: int | None = None
    active: bool = True


@dataclass(frozen=True)
class ScoringPolicy:
    tier_weights: dict[int, float]
    freshness_floor: float = 0.2
    half_life_days: float | None = 90
    version_mismatch_factor: float = 0.4
    unverified_quote_factor: float = 0.5
    community_cap: float = 1.2
    auto_verify_official: bool = True
    auto_verify_trusted_explicit: bool = True
    trusted_explicit_min_tier: int = 3
    corroboration_verify_enabled: bool = False
    corroboration_verify_groups: int = 3
    corroboration_verify_tier: int = 3
    corroborated_groups: int = 2
    corroborated_tier: int = 2


def policy_for(cfg: ProfileConfig, category_key: str | None) -> ScoringPolicy:
    half_life: float | None = 90
    if category_key is not None:
        for cat, _ in cfg.flat_categories():
            if cat.key == category_key:
                half_life = cat.settings.half_life_days
                break
    t = cfg.trust
    vp = t.verification_policy
    return ScoringPolicy(
        tier_weights=dict(t.tier_weights),
        freshness_floor=t.freshness_floor,
        half_life_days=half_life,
        version_mismatch_factor=t.version_mismatch_factor,
        unverified_quote_factor=t.unverified_quote_factor,
        community_cap=t.community_cap,
        auto_verify_official=vp.auto_verify_official,
        auto_verify_trusted_explicit=vp.auto_verify_trusted_explicit,
        trusted_explicit_min_tier=vp.trusted_explicit_min_tier,
        corroboration_verify_enabled=vp.auto_verify_corroboration.enabled,
        corroboration_verify_groups=vp.auto_verify_corroboration.min_groups,
        corroboration_verify_tier=vp.auto_verify_corroboration.min_tier,
        corroborated_groups=vp.corroborated.min_groups if vp.corroborated.enabled else 10**6,
        corroborated_tier=vp.corroborated.min_tier,
    )


def freshness(age_days: float, half_life_days: float | None, floor: float) -> float:
    if half_life_days is None or half_life_days <= 0:
        return 1.0
    return float(max(floor, 0.5 ** (max(age_days, 0.0) / half_life_days)))


def evidence_weight(e: EvidenceFacts, policy: ScoringPolicy, now: datetime) -> float:
    w = policy.tier_weights.get(e.tier, 0.0)
    w *= freshness((now - e.evidence_at).total_seconds() / 86400, policy.half_life_days, policy.freshness_floor)
    if e.version_match is False:
        w *= policy.version_mismatch_factor
    if not (e.quote_verified or e.human):
        w *= policy.unverified_quote_factor
    return w


def _aggregate(items: list[EvidenceFacts], policy: ScoringPolicy, now: datetime) -> tuple[float, dict[str, float]]:
    """Σ over independence groups of the best evidence in each group; community groups capped."""
    best: dict[str, float] = {}
    group_tier: dict[str, int] = {}
    for e in items:
        w = evidence_weight(e, policy, now)
        if w > best.get(e.group, -1.0):
            best[e.group] = w
        group_tier[e.group] = max(group_tier.get(e.group, 0), e.tier)
    community = sum(w for g, w in best.items() if group_tier[g] <= 2)
    strong = sum(w for g, w in best.items() if group_tier[g] > 2)
    return strong + min(community, policy.community_cap), best


def _groups_at_least(items: list[EvidenceFacts], tier: int) -> int:
    return len({e.group for e in items if e.tier >= tier})


@dataclass(frozen=True)
class Derived:
    verification: Verification
    basis: Basis | None
    support_score: float
    origins: list[str]
    is_public: bool
    audience_channel_ids: list[int]
    last_evidence_at: datetime | None
    has_support: bool
    summary: dict[str, Any] = field(default_factory=dict)


def derive(
    evidence: list[EvidenceFacts],
    *,
    human_verified: bool,
    open_conflict: bool,
    policy: ScoringPolicy,
    now: datetime,
    contra_weight: float = 0.5,
) -> Derived:
    active = [e for e in evidence if e.active]
    sup = [e for e in active if e.stance == "supports"]
    con = [e for e in active if e.stance == "contradicts"]
    s_sup, sup_groups = _aggregate(sup, policy, now)
    s_con, con_groups = _aggregate(con, policy, now)

    newest_official_sup = max((e.evidence_at for e in sup if e.tier >= 4), default=None)
    newest_official_con = max((e.evidence_at for e in con if e.tier >= 4), default=None)
    # Explicit capture (/kb add, 📌, "Add to knowledge") by a trusted member is a human endorsement,
    # even when an LLM rephrased the text: grounding is enforced by the verbatim quote check.
    trusted_explicit = any(
        e.endorser_tier is not None and e.endorser_tier >= policy.trusted_explicit_min_tier for e in sup
    )

    verification: Verification
    basis: Basis | None = None
    if human_verified:
        verification, basis = "verified", "human"
    elif open_conflict:
        verification = "disputed"
    elif policy.auto_verify_trusted_explicit and trusted_explicit:
        verification, basis = "verified", "human"
    elif (
        policy.auto_verify_official
        and newest_official_sup is not None
        and (newest_official_con is None or newest_official_con < newest_official_sup)
    ):
        verification, basis = "verified", "official_source"
    elif (
        policy.corroboration_verify_enabled
        and _groups_at_least(sup, policy.corroboration_verify_tier) >= policy.corroboration_verify_groups
        and not con
    ):
        verification, basis = "verified", "corroboration"
    elif _groups_at_least(sup, policy.corroborated_tier) >= policy.corroborated_groups:
        verification = "corroborated"
    else:
        verification = "unverified"

    audiences = sorted({e.audience_channel_id for e in sup if e.audience_channel_id is not None})
    is_public = any(e.audience_channel_id is None for e in sup) or (human_verified and not audiences)
    last = max((e.evidence_at for e in active), default=None)
    summary = {
        "groups_sup": len(sup_groups),
        "groups_con": len(con_groups),
        "max_tier": max((e.tier for e in sup), default=None),
        "newest_evidence_at": last.isoformat() if last else None,
        "origins": sorted({e.origin for e in sup}),
        "s_sup": round(s_sup, 4),
        "s_con": round(s_con, 4),
        "basis": basis,
    }
    return Derived(
        verification=verification,
        basis=basis,
        support_score=round(s_sup - contra_weight * s_con, 6),
        origins=sorted({e.origin for e in sup}),
        is_public=is_public,
        audience_channel_ids=audiences,
        last_evidence_at=last,
        has_support=bool(sup),
        summary=summary,
    )


def allowed_states(min_state: str) -> list[str]:
    """States a scope may return. Disputed claims are shown (both sides) only without a minimum."""
    if min_state == "verified":
        return ["verified"]
    if min_state == "corroborated":
        return ["corroborated", "verified"]
    return ["unverified", "corroborated", "verified", "disputed"]
