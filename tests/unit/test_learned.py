from __future__ import annotations

import numpy as np

from guru.core.learned import GateModel, fit_gate


def _data(n: int, noise: float, seed: int = 0) -> tuple[list[list[float]], list[int]]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 6))
    logits = 3 * X[:, 0] - 2 * X[:, 1] + rng.normal(scale=noise, size=n)
    y = (logits > 0).astype(int)
    return X.tolist(), y.tolist()


def test_not_enough_labels_means_no_automation() -> None:
    X, y = _data(50, 0.1)
    assert fit_gate(X, y, min_labels=200) is None


def test_separable_data_enables_auto_with_high_precision() -> None:
    X, y = _data(600, 0.1)
    gate = fit_gate(X, y, target_precision=0.95, min_labels=200)
    assert gate is not None and gate.auto_enabled
    Xt, yt = _data(400, 0.1, seed=1)
    decisions = [(gate.decide(x), label) for x, label in zip(Xt, yt, strict=True)]
    auto = [(d, label) for d, label in decisions if d is not None]
    correct = sum(1 for d, label in auto if (d == "keep") == (label == 1))
    assert len(auto) > 100 and correct / len(auto) >= 0.93
    assert GateModel.from_json(gate.to_json()).decide(Xt[0]) == gate.decide(Xt[0])


def test_random_labels_never_enable_auto() -> None:
    rng = np.random.default_rng(3)
    X = rng.normal(size=(500, 6)).tolist()
    y = rng.integers(0, 2, size=500).tolist()
    gate = fit_gate(X, y, target_precision=0.95, min_labels=200)
    assert gate is not None and not gate.auto_enabled
