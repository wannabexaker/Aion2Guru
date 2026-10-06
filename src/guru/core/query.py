"""Query parsing, scopes, coverage and deterministic ranking. Pure."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from guru.core.knowledge import allowed_states, freshness
from guru.core.text import normalize

ScopeName = Literal["faq", "verified", "internal", "web", "all"]


@dataclass(frozen=True)
class ScopeSpec:
    name: ScopeName
    origins: list[str] | None  # None = any origin
    min_state: str
    faq_only: bool = False

    @property
    def states(self) -> list[str]:
        return allowed_states(self.min_state)


def resolve_scope(name: str, default_min_state: str) -> ScopeSpec:
    if name == "faq":
        return ScopeSpec("faq", None, "verified", faq_only=True)
    if name == "verified":
        return ScopeSpec("verified", None, "verified")
    if name == "internal":
        return ScopeSpec("internal", ["discord", "manual", "import"], default_min_state)
    if name == "web":
        return ScopeSpec("web", ["web"], default_min_state)
    return ScopeSpec("all", None, default_min_state)


@dataclass(frozen=True)
class ParsedQuery:
    text: str
    scope: ScopeName | None = None
    category: str | None = None
    region: str | None = None


def parse_question(
    raw: str,
    scope_tokens: dict[str, list[str]],
    category_keys: set[str],
    region_values: set[str] = frozenset(),  # type: ignore[assignment]
) -> ParsedQuery:
    """Leading `scope:` tokens, `#category` anywhere, `region:XX`. Unknown tokens stay in the question."""
    token_to_scope = {normalize(t): s for s, toks in scope_tokens.items() for t in toks}
    regions = {r.upper() for r in region_values}
    words = raw.split()
    scope: str | None = None
    category: str | None = None
    region: str | None = None
    kept: list[str] = []
    leading = True
    for word in words:
        low = word.strip().lower()
        if leading and low.endswith(":") and normalize(low[:-1]) in token_to_scope and scope is None:
            scope = token_to_scope[normalize(low[:-1])]
            continue
        if low.startswith("scope:") and normalize(low[6:]) in token_to_scope:
            scope = token_to_scope[normalize(low[6:])]
            continue
        if low.startswith("region:") and low[7:].upper() in regions:
            region = low[7:].upper()
            continue
        if low.startswith("#") and low[1:].rstrip("?,.!;") in category_keys:
            category = low[1:].rstrip("?,.!;")
            continue
        leading = False
        kept.append(word)
    return ParsedQuery(" ".join(kept).strip(), scope, category, region)  # type: ignore[arg-type]


# ---------------------------------------------------------------- coverage & ranking


def _token_match(q: str, d: str) -> bool:
    if q == d:
        return True
    if len(q) >= 4 and len(d) >= 4 and (d.startswith(q) or q.startswith(d)):
        return True
    return len(q) >= 5 and len(d) >= 5 and q[:5] == d[:5]


def coverage(query_tokens: list[str], doc_norm: str, satisfied: frozenset[str] = frozenset()) -> float:
    """Fraction of query content tokens present in the document (prefix/stem tolerant).

    `satisfied` tokens count as present: e.g. a Greek category alias ("κλαση") when the document
    belongs to that category — the alias already resolved the concept across languages.
    """
    if not query_tokens:
        return 0.0
    doc = doc_norm.split()
    found = sum(1 for q in query_tokens if q in satisfied or any(_token_match(q, d) for d in doc))
    return found / len(query_tokens)


@dataclass
class Candidate:
    claim_id: int
    statement: str
    search_text: str
    lang: str
    category_id: int | None
    verification: str
    basis: str | None
    needs_review: bool
    last_evidence_at: datetime | None
    summary: dict[str, object]
    ranks: dict[str, int] = field(default_factory=dict)  # list name → 1-based rank
    coverage: float = 0.0
    score: float = 0.0


@dataclass(frozen=True)
class RankingParams:
    rrf_k: int
    state_factors: dict[str, float]
    half_life_days: dict[int | None, float | None]  # category id → half life
    freshness_floor: float
    boost_categories: frozenset[int]
    soft_category_factor: float = 0.7


def rank(candidates: list[Candidate], params: RankingParams, now: datetime) -> list[Candidate]:
    for c in candidates:
        rrf = sum(1.0 / (params.rrf_k + r) for r in c.ranks.values())
        state = params.state_factors.get(c.verification, 0.5)
        age = 0.0 if c.last_evidence_at is None else (now - c.last_evidence_at).total_seconds() / 86400
        fresh = freshness(age, params.half_life_days.get(c.category_id, 90), params.freshness_floor)
        cat = 1.0
        if params.boost_categories and c.category_id not in params.boost_categories:
            cat = params.soft_category_factor
        # Coverage gates relevance; sqrt keeps partial matches competitive but below full ones.
        c.score = rrf * state * fresh * cat * math.sqrt(max(c.coverage, 0.0))
    return sorted(candidates, key=lambda c: (-c.score, c.claim_id))


def is_dominant(ranked: list[Candidate], ratio: float) -> bool:
    if len(ranked) < 2:
        return bool(ranked)
    return ranked[1].score <= 0 or ranked[0].score / ranked[1].score >= ratio
