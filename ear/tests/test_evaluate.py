"""Cheap checks of ear.evaluate: the probe signals, the scoring definitions, determinism, and
the numpy float network. The full evaluation (python -m ear.evaluate) is what REPORT.md uses."""

import random

import numpy as np

from ear import evaluate as ev
from ear import protosim as ps


def _alternates(edges):
    last = {}
    for t, p, v in edges:
        assert v == 1 - last.get(p, 0), "each pin starts low and toggles"
        last[p] = v


def test_probe_signals_are_well_formed():
    for name, edges in ev.probe_signals(seed=3, duration=100_000):
        assert edges, name
        assert edges == sorted(edges)
        assert all(0 <= t < 100_000 for t, _, _ in edges)
        _alternates(sorted(edges, key=lambda e: (e[1], e[0])))
    assert ev.pwm(20e3, 0.2, 10_000)[:4] == [(0, 0, 1), (500, 0, 0), (2500, 0, 1), (3000, 0, 0)]
    assert {p for _, p, _ in ev.random_edges(random.Random(1), 50, 4, 20_000)} == {0, 1, 2, 3}


def test_hidden_units_match_the_reference_model():
    m = ev.shipped()
    rng = np.random.default_rng(0)
    X = rng.integers(0, 64, (20, 72))
    h = ev.hidden_int(m, X)
    for x, row in zip(X, h):
        assert list(row) == m.infer(list(map(int, x))).hidden


def test_score_counts_gated_windows_as_unanswered():
    m = ev.shipped()
    X = np.zeros((4, 72), dtype=np.int64)
    edges = np.array([0, 5, 23, 24])
    s = ev.score(m, X, edges, truth=0)
    assert s.windows == 4
    ood_by_floor = max(ev.int_logits(np.array(m.w1), np.array(m.w2), X[:1], m.cfg.shift,
                                     np.array(m.bias))[0]) < m.cfg.floor
    assert s.answered == (0.0 if ood_by_floor else 0.25)     # only the 24-edge window passes
    assert sum(s.calls_all.values()) == 4


def test_coverage_counts_slow_uart_windows_and_is_deterministic():
    m = ev.shipped()
    pts = {"UART": [("2,400 baud", {"period": ps.CLOCK_HZ / 2400, "duplex": False})]}
    a = ev.coverage(m, captures=1, speeds=pts)
    b = ev.coverage(m, captures=1, speeds=pts)
    (label, name, mean_edges, s), = a
    assert label == "UART" and s.windows >= 20            # 30 ms of 1.3 ms windows
    assert mean_edges < ev.MIN_EDGES and s.answered < 0.2  # most are gated
    assert [(r[2], r[3].windows, r[3].answered) for r in a] == \
           [(r[2], r[3].windows, r[3].answered) for r in b]


def test_float_mlp_learns_a_separable_problem():
    rng = np.random.default_rng(1)
    y = rng.integers(0, 3, 1200)
    X = rng.normal(0, 1, (1200, 72)) + 4.0 * np.eye(72)[y]
    f = ev.float_mlp(X[:900], y[:900], X[900:1050], y[900:1050], hidden=8, epochs=300)
    assert (f(X[1050:]) == y[1050:]).mean() > 0.95
