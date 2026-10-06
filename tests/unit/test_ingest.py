from __future__ import annotations

from datetime import UTC, datetime

from guru.core.config import PrefilterCfg
from guru.core.ingest import (
    ExtractionOutput,
    PrefilterInput,
    SourceMessage,
    is_question,
    prefilter,
    quote_found,
    teach_prefix,
    validate_extraction,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
CFG = PrefilterCfg()


def test_is_question_multilingual() -> None:
    for q in ("where is it?", "Πού είναι;", "pou einai", "πως κανω enchant", "how to enchant", "ti kanei auto"):
        assert is_question(q), q
    for s in ("The boss spawns at 12:00.", "Ο boss βγαίνει στις 12", "o boss vgainei stis 12"):
        assert not is_question(s), s


def test_teach_prefix() -> None:
    assert teach_prefix("μάθε: ο boss βγαίνει κάθε 4 ώρες") == "ο boss βγαίνει κάθε 4 ώρες"
    assert teach_prefix("learn:  Fire Temple resets daily") == "Fire Temple resets daily"
    assert teach_prefix("where is it?") is None


def test_prefilter_decisions() -> None:
    def run(text: str, **kw: object) -> str:
        return prefilter(PrefilterInput(text=text, author_tier=kw.pop("tier", 2), **kw), CFG).decision  # type: ignore[arg-type]

    assert run("lol") == "drop"
    assert run("<@123> 😂 https://x.y") == "drop"
    assert run("where does the boss spawn?") == "context"
    assert run("I really like this game a lot honestly") == "context"
    assert run("The Fire Temple boss respawns every 4 hours", entity_hits=1) == "candidate"
    assert run("Enchanting to +10 costs about 300k kinah", alias_hits=1) == "candidate"
    assert run("trust me this works fine for everyone", tier=3) == "candidate"


def _src(sid: str, text: str, candidate: bool = True) -> SourceMessage:
    return SourceMessage(sid, int(sid[1:]), 1, text, 2, f"discord:user:{sid}", NOW, candidate=candidate)


def _validate(claims: list[dict[str, object]], sources: list[SourceMessage]):  # type: ignore[no-untyped-def]
    out = ExtractionOutput.model_validate({"claims": claims})
    return validate_extraction(
        out,
        {s.sid: s for s in sources},
        {"content", "general"},
        {"fact", "tip", "procedure"},
        default_category="general",
        version_patterns=[r"(?i)\bpatch\s*(\d+\.\d+)"],
    )


def test_validation_accepts_grounded_claim_with_greek_source() -> None:
    sources = [
        _src("m1", "ποιος ξέρει πότε βγαίνει;", candidate=False),
        _src("m2", "Ο boss του Fire Temple βγαίνει κάθε 4 ώρες από το patch 1.2"),
    ]
    valid, rejected = _validate(
        [
            {
                "statement": "The Fire Temple boss respawns every 4 hours (since patch 1.2).",
                "type": "fact",
                "category": "content",
                "source_ids": ["m2"],
                "quotes": ["βγαίνει κάθε 4 ώρες από το patch 1.2"],
            }
        ],
        sources,
    )
    assert not rejected and len(valid) == 1
    assert valid[0].category_key == "content" and valid[0].version == "1.2"
    assert valid[0].evidence[0][0].sid == "m2"


def test_validation_rejections() -> None:
    sources = [_src("m1", "Boss spawns every 4 hours near the lake"), _src("m2", "what time?", candidate=False)]
    base = {"type": "fact", "category": "content", "source_ids": ["m1"], "quotes": ["spawns every 4 hours"]}
    cases = {
        "V7_type": {**base, "statement": "I think the boss is fun to fight", "type": "opinion"},
        "V2_sources": {**base, "statement": "Boss spawns every 4 hours.", "source_ids": ["m9"]},
        "V3_quotes": {**base, "statement": "Boss spawns every 4 hours.", "quotes": ["spawns at midnight only"]},
        "V8_numbers": {**base, "statement": "Boss spawns every 6 hours near the lake."},
        "V6_links": {**base, "statement": "Boss spawns every 4 hours, see https://evil.example"},
    }
    for rule, claim in cases.items():
        valid, rejected = _validate([claim], sources)
        assert not valid and rejected[0].rule == rule, rule
    # context-only messages cannot be evidence
    valid, rejected = _validate(
        [{**base, "statement": "Players ask about time.", "source_ids": ["m2"], "quotes": ["what time"]}], sources
    )
    assert rejected[0].rule == "V2_sources"


def test_unknown_category_falls_back() -> None:
    valid, _ = _validate(
        [
            {
                "statement": "Boss spawns every 4 hours.",
                "type": "fact",
                "category": "zzz",
                "source_ids": ["m1"],
                "quotes": ["Boss spawns every 4 hours"],
            }
        ],
        [_src("m1", "Boss spawns every 4 hours")],
    )
    assert valid[0].category_key == "general"


def test_quote_found_fuzzy_but_strict() -> None:
    assert quote_found("spawns every 4 hours", "the boss SPAWNS every 4 hours!!", 0.9)
    assert quote_found("βγαινει καθε 4 ωρες", "Βγαίνει κάθε 4 ώρες στο δάσος", 0.9)
    assert not quote_found("drops a legendary sword", "the boss spawns every 4 hours", 0.9)
    assert not quote_found("ok", "ok", 0.9)  # too short to be evidence
