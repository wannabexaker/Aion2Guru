"""Learned review gate (D-27): imitate moderator keep/reject decisions, only where it is measurably safe.

- Logistic regression (numpy, L2) on [embedding ‖ scalar features].
- Time-ordered split: train on older labels, evaluate on the newest ones (no leakage from the future).
- Separate thresholds for auto-keep and auto-reject, each chosen so held-out precision ≥ target.
- Everything between the thresholds stays with humans (active learning).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

Decision = Literal["keep", "reject"]


@dataclass
class GateModel:
    weights: list[float]
    bias: float
    mean: list[float]
    std: list[float]
    keep_threshold: float | None  # p ≥ t → auto keep
    reject_threshold: float | None  # p ≤ t → auto reject
    n_labels: int
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def auto_enabled(self) -> bool:
        return self.keep_threshold is not None or self.reject_threshold is not None

    def prob(self, x: list[float]) -> float:
        v = (np.asarray(x, dtype=np.float64) - np.asarray(self.mean)) / np.asarray(self.std)
        return float(_sigmoid(float(v @ np.asarray(self.weights)) + self.bias))

    def decide(self, x: list[float]) -> Decision | None:
        p = self.prob(x)
        if self.keep_threshold is not None and p >= self.keep_threshold:
            return "keep"
        if self.reject_threshold is not None and p <= self.reject_threshold:
            return "reject"
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "weights": self.weights, "bias": self.bias, "mean": self.mean, "std": self.std,
            "keep_threshold": self.keep_threshold, "reject_threshold": self.reject_threshold,
            "n_labels": self.n_labels, "metrics": self.metrics,
        }  # fmt: skip

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> GateModel:
        return cls(**d)


def _sigmoid(z: Any) -> Any:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def train_logreg(
    X: np.ndarray, y: np.ndarray, *, l2: float = 1.0, epochs: int = 400, lr: float = 0.5
) -> tuple[np.ndarray, float]:
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(epochs):
        p = _sigmoid(X @ w + b)
        grad_w = X.T @ (p - y) / n + l2 * w / n
        grad_b = float(np.mean(p - y))
        w -= lr * grad_w
        b -= lr * grad_b
    return w, b


def _pick_threshold(p: np.ndarray, y: np.ndarray, target: float, positive: bool, min_support: int) -> float | None:
    """Most permissive threshold whose held-out precision for the decision is ≥ target."""
    candidates = sorted(set(np.round(p, 4).tolist()), reverse=positive)
    best: float | None = None
    for t in candidates:
        mask = p >= t if positive else p <= t
        support = int(mask.sum())
        if support < min_support:
            continue
        correct = int((y[mask] == (1 if positive else 0)).sum())
        if correct / support >= target:
            best = float(t)
        else:
            break  # precision only gets worse as the threshold relaxes further
    return best


def fit_gate(
    X: list[list[float]],
    y: list[int],
    *,
    target_precision: float = 0.95,
    min_labels: int = 200,
    eval_fraction: float = 0.2,
    min_support: int = 10,
    min_confidence: float = 0.8,
) -> GateModel | None:
    """X, y must be in chronological order (oldest first)."""
    n = len(y)
    if n < min_labels or len(set(y)) < 2:
        return None
    Xa = np.asarray(X, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    split = int(n * (1 - eval_fraction))
    mean = Xa[:split].mean(axis=0)
    std = Xa[:split].std(axis=0) + 1e-6
    Xs = (Xa - mean) / std
    w, b = train_logreg(Xs[:split], ya[:split])
    p_eval = _sigmoid(Xs[split:] @ w + b)
    y_eval = ya[split:]
    keep_t = _pick_threshold(p_eval, y_eval, target_precision, True, min_support)
    reject_t = _pick_threshold(p_eval, y_eval, target_precision, False, min_support)
    # Never auto-decide near the boundary, however good the held-out precision looks.
    if keep_t is not None:
        keep_t = max(keep_t, min_confidence)
    if reject_t is not None:
        reject_t = min(reject_t, 1 - min_confidence)
    if keep_t is not None and reject_t is not None and reject_t >= keep_t:
        reject_t = None  # overlapping thresholds → only the keep side is trusted
    auto = np.zeros_like(y_eval, dtype=bool)
    if keep_t is not None:
        auto |= p_eval >= keep_t
    if reject_t is not None:
        auto |= p_eval <= reject_t
    pred = (p_eval >= 0.5).astype(float)
    metrics = {
        "eval_n": len(y_eval),
        "eval_accuracy": float((pred == y_eval).mean()) if len(y_eval) else None,
        "auto_coverage": float(auto.mean()) if len(y_eval) else 0.0,
        "target_precision": target_precision,
    }
    # Final model is retrained on all labels; thresholds come from the honest held-out evaluation.
    w_all, b_all = train_logreg(Xs, ya)
    return GateModel(
        weights=w_all.tolist(), bias=float(b_all), mean=mean.tolist(), std=std.tolist(),
        keep_threshold=keep_t, reject_threshold=reject_t, n_labels=n, metrics=metrics,
    )  # fmt: skip
