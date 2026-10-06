"""Prometheus metrics (exposed by the API role at /metrics)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

JOBS_PROCESSED = Counter("guru_jobs_processed_total", "Jobs processed", ["kind", "outcome"])

QUERIES = Counter("guru_queries_total", "Questions handled", ["answered_by"])
QUERY_LATENCY = Histogram(
    "guru_query_latency_seconds",
    "End-to-end question latency",
    ["answered_by"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)
STAGE_LATENCY = Histogram(
    "guru_query_stage_seconds",
    "Latency per pipeline stage",
    ["stage"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 5, 20),
)
ACCESS_DENIED = Counter("guru_access_denied_total", "Messages rejected by access enforcement", ["action"])
RATE_LIMITED = Counter("guru_rate_limited_total", "Requests rejected by rate limits", ["bucket"])
LLM_TOKENS = Counter("guru_llm_tokens_total", "LLM tokens", ["task", "kind"])
INGEST_DECISIONS = Counter("guru_ingest_decisions_total", "Prefilter decisions", ["decision"])
REVIEW_DECISIONS = Counter("guru_review_decisions_total", "Moderator/auto review decisions", ["kind", "decision"])
