"""Ingestion decisions (DESIGN §6): question detection, prefilter, extraction validation. Pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import re2
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from guru.core.config import PrefilterCfg
from guru.core.text import normalize, tokens

# ---------------------------------------------------------------- questions

_INTERROGATIVES = frozenset(
    """what where when why how who which whose whom can could does do did is are was were will would should
    τι πού που πώς πως πότε ποτε γιατί γιατι ποιος ποια ποιο ποιοι ποιες πόσο ποσο πόσα ποσα πόσες ποιον μπορώ
    μπορω υπάρχει υπαρχει ξέρει ξερει ξέρετε ξερετε
    ti pou poy pws pos pote giati gt poios poia poio poioi posa poso posoi mporw mporo yparxei uparxei kserei
    kserete""".split()
)
_TEACH_PREFIXES = ("learn:", "μάθε:", "μαθε:", "mathe:", "note:", "σημείωσε:", "shmeiwse:")


def is_question(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("?", ";", ";")):
        return True
    toks = tokens(stripped)
    return bool(toks) and toks[0] in {normalize(w) for w in _INTERROGATIVES}


def teach_prefix(text: str) -> str | None:
    """`learn: <fact>` / `μάθε: <fact>` → the fact text; otherwise None."""
    low = text.strip().lower()
    for prefix in _TEACH_PREFIXES:
        if low.startswith(prefix):
            return text.strip()[len(prefix) :].strip()
    return None


# ---------------------------------------------------------------- prefilter

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<[@#][!&]?\d+>|<a?:\w+:\d+>")
_UNIT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|h|hr|hrs|m|min|mins|s|sec|k|kk|x|lvl|level|ώρες|λεπτά|lepta|wres)\b",
                      re.IGNORECASE)  # fmt: skip

Decision = Literal["drop", "context", "candidate"]


@dataclass(frozen=True)
class PrefilterInput:
    text: str
    author_tier: int
    alias_hits: int = 0  # entity/category/profile alias hits
    entity_hits: int = 0
    is_reply_to_question: bool = False
    signal_bonus: int = 0


@dataclass(frozen=True)
class PrefilterResult:
    decision: Decision
    score: int
    reasons: tuple[str, ...] = ()


def _strip_noise(text: str) -> str:
    return _MENTION_RE.sub(" ", _URL_RE.sub(" ", text)).strip()


def prefilter(inp: PrefilterInput, cfg: PrefilterCfg) -> PrefilterResult:
    text = _strip_noise(inp.text)
    letters = sum(ch.isalpha() for ch in text)
    if letters < 3:
        return PrefilterResult("drop", 0, ("no_text",))
    if len(text) > cfg.max_chars:
        return PrefilterResult("context", 0, ("too_long",))
    if is_question(text):
        return PrefilterResult("context", 0, ("question",))
    if len(text) < cfg.min_chars:
        return PrefilterResult("drop", 0, ("too_short",))
    w = cfg.weights
    score = inp.signal_bonus
    reasons: list[str] = []
    if inp.entity_hits:
        score += w.get("entity_alias_hit", 2)
        reasons.append("entity_alias_hit")
    elif inp.alias_hits:
        score += w.get("keyword_hit", 1)
        reasons.append("keyword_hit")
    if _UNIT_RE.search(text) or any(t.isdigit() for t in tokens(text)):
        score += w.get("number_or_unit", 1)
        reasons.append("number_or_unit")
    if inp.is_reply_to_question:
        score += w.get("reply_to_question", 1)
        reasons.append("reply_to_question")
    if inp.author_tier >= 3:
        score += w.get("author_tier_ge_3", 2)
        reasons.append("author_tier_ge_3")
    decision: Decision = "candidate" if score >= cfg.threshold else "context"
    return PrefilterResult(decision, score, tuple(reasons))


# ---------------------------------------------------------------- extraction output & validation

ClaimType = Literal["fact", "tip", "procedure", "opinion", "question", "other"]


class ExtractedClaim(BaseModel):
    statement: str = Field(min_length=5, max_length=500)
    type: ClaimType
    category: str
    source_ids: list[str] = Field(min_length=1, max_length=6)
    quotes: list[str] = Field(min_length=1, max_length=6)
    version_hint: str | None = None


class ExtractionOutput(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=12)


def extraction_schema(category_keys: list[str]) -> dict[str, object]:
    """JSON Schema for constrained decoding. Category is an enum of the profile's keys."""
    return {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["fact", "tip", "procedure", "opinion", "question", "other"],
                        },
                        "category": {"type": "string", "enum": category_keys},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "quotes": {"type": "array", "items": {"type": "string"}},
                        "version_hint": {"type": ["string", "null"]},
                    },
                    "required": ["statement", "type", "category", "source_ids", "quotes"],
                },
            }
        },
        "required": ["claims"],
    }


@dataclass(frozen=True)
class SourceMessage:
    sid: str
    observation_id: int
    rev: int
    text: str
    author_tier: int
    group: str
    published_at: datetime
    candidate: bool = True
    audience_channel_id: int | None = None
    endorsed_by: int | None = None
    endorser_tier: int | None = None


@dataclass
class ValidClaim:
    statement: str
    claim_type: str
    category_key: str
    evidence: list[tuple[SourceMessage, str]] = field(default_factory=list)  # (source, verified quote)
    version: str | None = None


@dataclass(frozen=True)
class Rejection:
    index: int
    rule: str
    detail: str = ""


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM_RE.findall(text)}


def quote_found(quote: str, source: str, min_ratio: float) -> bool:
    q, s = normalize(quote), normalize(source)
    if len(q) < 6:
        return False
    if q in s:
        return True
    return fuzz.partial_ratio(q, s) >= min_ratio * 100


def validate_extraction(
    out: ExtractionOutput,
    sources: dict[str, SourceMessage],
    category_keys: set[str],
    allowed_types: set[str],
    *,
    default_category: str,
    quote_min_ratio: float = 0.9,
    version_patterns: list[str] | None = None,
) -> tuple[list[ValidClaim], list[Rejection]]:
    valid: list[ValidClaim] = []
    rejected: list[Rejection] = []
    compiled = [re2.compile(p) for p in (version_patterns or [])]
    for i, c in enumerate(out.claims):
        # V7 claim type
        if c.type not in allowed_types:
            rejected.append(Rejection(i, "V7_type", c.type))
            continue
        statement = " ".join(c.statement.split())
        # V6 length / no injected links or mentions
        if not 10 <= len(statement) <= 400:
            rejected.append(Rejection(i, "V6_length"))
            continue
        if (
            "<@" in statement
            or "<#" in statement
            or ("http" in statement and not any("http" in sources[s].text for s in c.source_ids if s in sources))
        ):
            rejected.append(Rejection(i, "V6_links"))
            continue
        # V2 sources must exist in the window and be candidates
        srcs = [sources[s] for s in c.source_ids if s in sources and sources[s].candidate]
        if not srcs:
            rejected.append(Rejection(i, "V2_sources", ",".join(c.source_ids)))
            continue
        # V3 every evidence needs a verbatim quote from that message
        evidence: list[tuple[SourceMessage, str]] = []
        for src in srcs:
            for q in c.quotes:
                if quote_found(q, src.text, quote_min_ratio):
                    evidence.append((src, q.strip()))
                    break
        if not evidence:
            rejected.append(Rejection(i, "V3_quotes"))
            continue
        # V8 every number in the statement must appear in its source messages (anti-hallucination)
        source_numbers = set().union(*(_numbers(s.text) for s, _ in evidence))
        missing = _numbers(statement) - source_numbers
        if missing:
            rejected.append(Rejection(i, "V8_numbers", ",".join(sorted(missing))))
            continue
        # V4 category
        category = c.category if c.category in category_keys else default_category
        version = None
        for pattern in compiled:
            for _, quote in evidence:
                m = pattern.search(quote)
                if m:
                    version = m.group(1) if m.groups() else m.group(0)
                    break
            if version:
                break
        valid.append(ValidClaim(statement, c.type, category, evidence, version))
    return valid, rejected
