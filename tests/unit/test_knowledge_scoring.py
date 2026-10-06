from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from guru.core.knowledge import EvidenceFacts, ScoringPolicy, allowed_states, derive, evidence_weight, freshness

NOW = datetime(2026, 10, 1, tzinfo=UTC)
POLICY = ScoringPolicy(tier_weights={0: 0.0, 1: 0.25, 2: 0.5, 3: 0.8, 4: 1.0}, half_life_days=30)


def ev(tier: int, group: str, stance: str = "supports", days: float = 0, **kw: object) -> EvidenceFacts:
    return EvidenceFacts(
        stance=stance,  # type: ignore[arg-type]
        tier=tier,
        group=group,
        origin=str(kw.pop("origin", "discord")),
        evidence_at=NOW - timedelta(days=days),
        **kw,  # type: ignore[arg-type]
    )


def d(evidence: list[EvidenceFacts], **kw: object) -> str:
    args = {"human_verified": False, "open_conflict": False, "policy": POLICY, "now": NOW} | kw
    return derive(evidence, **args).verification  # type: ignore[arg-type]


def test_freshness_half_life_and_floor() -> None:
    assert freshness(0, 30, 0.2) == 1.0
    assert abs(freshness(30, 30, 0.2) - 0.5) < 1e-9
    assert freshness(10_000, 30, 0.2) == 0.2
    assert freshness(10_000, None, 0.2) == 1.0


def test_single_community_source_is_unverified() -> None:
    assert d([ev(2, "discord:user:1")]) == "unverified"


def test_two_independent_groups_corroborate() -> None:
    assert d([ev(2, "discord:user:1"), ev(2, "discord:user:2")]) == "corroborated"


def test_same_group_does_not_corroborate() -> None:
    assert d([ev(2, "site:wiki"), ev(3, "site:wiki")]) == "unverified"


def test_official_source_verifies_unless_newer_official_contradicts() -> None:
    assert d([ev(4, "org:ncsoft", days=5)]) == "verified"
    assert d([ev(4, "org:ncsoft", days=5), ev(4, "org:ncsoft", "contradicts", days=1)]) == "unverified"


def test_trusted_explicit_capture_verifies() -> None:
    assert d([ev(2, "discord:user:1", human=True, endorser_tier=3)]) == "verified"
    assert d([ev(2, "discord:user:1", human=True, endorser_tier=2)]) == "unverified"
    # 📌 by a trusted member on an LLM-extracted claim is still an endorsement.
    assert d([ev(2, "discord:user:1", endorser_tier=3)]) == "verified"
    # Passively extracted from a trusted author (no endorsement) is not.
    assert d([ev(3, "discord:user:1")]) == "unverified"


def test_conflict_dominates_everything_but_human_verification() -> None:
    evidence = [ev(4, "org:ncsoft")]
    assert d(evidence, open_conflict=True) == "disputed"
    assert d(evidence, open_conflict=True, human_verified=True) == "verified"


def test_inactive_evidence_ignored() -> None:
    r = derive([ev(4, "org:ncsoft", active=False)], human_verified=False, open_conflict=False, policy=POLICY, now=NOW)
    assert r.verification == "unverified" and not r.has_support


def test_community_cap_limits_sybil_support() -> None:
    many = [ev(2, f"discord:user:{i}") for i in range(50)]
    r = derive(many, human_verified=False, open_conflict=False, policy=POLICY, now=NOW)
    official = derive(
        [ev(4, "org:x"), ev(3, "site:y")], human_verified=False, open_conflict=False, policy=POLICY, now=NOW
    )
    assert r.support_score <= POLICY.community_cap + 1e-9
    assert official.support_score > r.support_score


def test_visibility_public_if_any_public_support() -> None:
    restricted = ev(3, "discord:user:1", audience_channel_id=99)
    r = derive([restricted], human_verified=False, open_conflict=False, policy=POLICY, now=NOW)
    assert not r.is_public and r.audience_channel_ids == [99]
    r2 = derive(
        [restricted, ev(2, "discord:user:2")], human_verified=False, open_conflict=False, policy=POLICY, now=NOW
    )
    assert r2.is_public


def test_weight_penalties() -> None:
    base = evidence_weight(ev(3, "g"), POLICY, NOW)
    assert evidence_weight(ev(3, "g", version_match=False), POLICY, NOW) < base
    assert evidence_weight(ev(3, "g", quote_verified=False), POLICY, NOW) < base
    assert evidence_weight(ev(3, "g", quote_verified=False, human=True), POLICY, NOW) == base


def test_allowed_states() -> None:
    assert allowed_states("verified") == ["verified"]
    assert "disputed" in allowed_states("unverified")
    assert "unverified" not in allowed_states("corroborated")


evidence_strategy = st.builds(
    EvidenceFacts,
    stance=st.sampled_from(["supports", "contradicts"]),
    tier=st.integers(0, 4),
    group=st.sampled_from(["a", "b", "c", "d"]),
    origin=st.sampled_from(["discord", "web", "manual"]),
    evidence_at=st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2026, 9, 30)).map(
        lambda x: x.replace(tzinfo=UTC)
    ),
    quote_verified=st.booleans(),
    human=st.booleans(),
    endorser_tier=st.one_of(st.none(), st.integers(0, 4)),
    version_match=st.one_of(st.none(), st.booleans()),
    audience_channel_id=st.one_of(st.none(), st.just(5)),
    active=st.booleans(),
)


@given(st.lists(evidence_strategy, max_size=12), st.booleans(), st.booleans())
def test_invariants(evidence: list[EvidenceFacts], human: bool, conflict: bool) -> None:
    r = derive(evidence, human_verified=human, open_conflict=conflict, policy=POLICY, now=NOW)
    # verified ⇔ has a basis
    assert (r.verification == "verified") == (r.basis is not None)
    # human verification always wins; otherwise an open conflict means disputed
    if human:
        assert r.verification == "verified" and r.basis == "human"
    elif conflict:
        assert r.verification == "disputed"
    # without active supporting evidence nothing can be verified except by a human
    if not r.has_support and not human:
        assert r.verification in ("unverified", "disputed")
    # restricted-only support never becomes public
    sup = [e for e in evidence if e.active and e.stance == "supports"]
    if sup and all(e.audience_channel_id is not None for e in sup):
        assert not r.is_public
    # score is bounded: no single group contributes more than tier weight 1
    groups = {e.group for e in sup}
    assert r.support_score <= len(groups) + 1e-9
