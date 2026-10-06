"""`guru eval`: measure extraction and answering with the real models, to calibrate thresholds.

Nothing is written to the knowledge base: extraction runs prompt + validators only; queries bypass
the cache and the query log.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from guru.core.ingest import ExtractionOutput, SourceMessage, extraction_schema, validate_extraction
from guru.core.permissions import Principal
from guru.core.text import normalize
from guru.llm.client import LLMClient, LLMError
from guru.llm.prompts import extraction_prompt
from guru.services.profiles import ProfileState
from guru.services.query_service import QueryRequest, QueryService


def _contains_any(text: str, needles: list[str]) -> bool:
    norm = normalize(text)
    return any(normalize(n) in norm for n in needles)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def eval_extraction(state: ProfileState, llm: LLMClient, cases: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = state.config
    keys = sorted(state.category_ids)
    stats: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    latencies: list[float] = []
    details = []
    for n, case in enumerate(cases, start=1):
        sources = {
            f"m{i}": SourceMessage(f"m{i}", -i, 1, text, 2, f"user{i}", datetime.now(UTC))
            for i, text in enumerate(case["messages"], start=1)
        }
        prompt = extraction_prompt(cfg, list(sources.values()), {s: "user, tier 2" for s in sources})
        t0 = time.perf_counter()
        try:
            res = await llm.run(
                "extract",
                prompt.system,
                prompt.user,
                extraction_schema(keys),
                validate=lambda d: ExtractionOutput.model_validate(d),
            )
        except LLMError as exc:
            stats["llm_errors"] += 1
            details.append({"case": n, "error": str(exc)[:200]})
            continue
        latencies.append((time.perf_counter() - t0) * 1000)
        out = ExtractionOutput.model_validate(res.data)
        valid, rejected = validate_extraction(
            out, sources, set(keys), set(cfg.ingestion.allowed_claim_types),
            default_category="general" if "general" in keys else keys[0], quote_min_ratio=cfg.ingestion.quote_min_ratio,
        )  # fmt: skip
        rejections.update(r.rule for r in rejected)
        expected: list[str] = case.get("expect", [])
        statements = [v.statement for v in valid]
        stats["claims"] += len(valid)
        if expected:
            stats["expected_cases"] += 1
            found = [e for e in expected if any(_contains_any(s, [e]) for s in statements)]
            stats["expected_found"] += len(found)
            stats["expected_total"] += len(expected)
            stats["relevant_claims"] += sum(1 for s in statements if _contains_any(s, expected))
        else:
            stats["empty_cases"] += 1
            stats["false_claims_on_empty"] += len(valid)
        details.append({"case": n, "claims": statements, "rejected": [r.rule for r in rejected]})
    recall = stats["expected_found"] / stats["expected_total"] if stats["expected_total"] else None
    precision = stats["relevant_claims"] / max(1, stats["claims"] - stats["false_claims_on_empty"])
    return {
        "cases": len(cases),
        "claims": stats["claims"],
        "recall_expected_facts": recall,
        "precision_proxy": precision if stats["claims"] else None,
        "claims_on_noise_cases": stats["false_claims_on_empty"],
        "validator_rejections": dict(rejections),
        "llm_errors": stats["llm_errors"],
        "latency_ms_p50": _pct(latencies, 0.5),
        "latency_ms_p95": _pct(latencies, 0.95),
        "details": details,
    }


async def eval_queries(state: ProfileState, qs: QueryService, cases: list[dict[str, Any]]) -> dict[str, Any]:
    qs.cache_enabled = False
    admin = Principal(user_id=0, is_guild_admin=True)
    by: Counter[str] = Counter()
    latencies: list[float] = []
    tp = fn = fp = tn = keyword_hits = keyword_cases = 0
    details = []
    for case in cases:
        ans = await qs.answer(QueryRequest(state, admin, case["q"], state.guild_id, 0))
        by[ans.answered_by] += 1
        latencies.append(ans.stage_ms.get("total", 0.0))
        answered = ans.mode not in ("no_answer", "empty")
        should = bool(case.get("expect_answer", True))
        tp += answered and should
        fn += (not answered) and should
        fp += answered and not should
        tn += (not answered) and not should
        text = " ".join([ans.text or "", *(i.statement for i in ans.items), (ans.faq or {}).get("answer", "")])
        hit = None
        if case.get("expect_any"):
            keyword_cases += 1
            hit = _contains_any(text, case["expect_any"])
            keyword_hits += int(hit)
        details.append(
            {
                "q": case["q"],
                "mode": ans.mode,
                "route": ans.route,
                "hit": hit,
                "ms": round(ans.stage_ms.get("total", 0.0), 1),
            }
        )
    total = len(cases)
    return {
        "questions": total,
        "answered_when_expected": tp / max(1, tp + fn),
        "answered_when_not_expected": fp / max(1, fp + tn),
        "keyword_accuracy": keyword_hits / keyword_cases if keyword_cases else None,
        "answered_by": dict(by),
        "no_llm_share": 1 - by.get("llm", 0) / max(1, total),
        "latency_ms_p50": _pct(latencies, 0.5),
        "latency_ms_p95": _pct(latencies, 0.95),
        "latency_ms_mean": statistics.fmean(latencies) if latencies else 0.0,
        "details": details,
    }
