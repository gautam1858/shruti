"""Training pipeline: small, fast runs (the full run is `python -m shruti train`)."""

import numpy as np
import pytest

from ear import protosim as ps
from ear.dataset import build
from ear.mlp import Model
from ear.train import (TrainConfig, _class_weights, _fit_tau, _xent, calibrate, int_logits,
                       load, predict, refine, save, train, ternarize)


def test_ternarize():
    w = np.array([[0.9, -0.05, -0.8, 0.1]])
    assert ternarize(w).tolist() == [[1, 0, -1, 0]]


def test_vectorised_integer_model_matches_the_reference():
    rng = np.random.default_rng(0)
    T1 = rng.integers(-1, 2, (8, 72))
    T2 = rng.integers(-1, 2, (8, 8))
    bias = rng.integers(-128, 128, 8)
    X = rng.integers(0, 64, (50, 72))
    m = Model(T1.tolist(), T2.tolist(), bias=[int(b) for b in bias])
    m.cfg.shift = 3
    got = int_logits(T1, T2, X, 3, bias)
    for row, x in zip(got, X):
        assert row.tolist() == m.infer(x.tolist()).logits


def _loss(T1, T2, b, s, X, y):
    Z = int_logits(T1, T2, X, s, b)
    sw = _class_weights(y)
    return _xent(Z, y, _fit_tau(Z, y, sw), sw)


def test_refine_lowers_the_loss_and_returns_a_valid_exact_model():
    rng = np.random.default_rng(1)
    X = rng.integers(0, 64, (400, 72))
    teacher = rng.integers(-1, 2, (8, 72))
    y = np.argmax(X @ teacher.T, axis=1)                    # a target the network can fit
    T1, T2 = rng.integers(-1, 2, (8, 72)), rng.integers(-1, 2, (8, 8))
    b = rng.integers(-20, 20, 8)
    before = _loss(T1, T2, b, 3, X, y)
    R1, R2, Rb = refine(T1, T2, b, 3, X, y, sweeps=3, seed=0)
    assert _loss(R1, R2, Rb, 3, X, y) < before
    assert np.mean(predict(int_logits(R1, R2, X, 3, Rb)) == y) > \
        np.mean(predict(int_logits(T1, T2, X, 3, b)) == y)
    assert set(np.unique(R1)) <= {-1, 0, 1} and set(np.unique(R2)) <= {-1, 0, 1}
    assert all(-128 <= v <= 127 for v in Rb)
    m = Model(R1.tolist(), R2.tolist(), bias=[int(v) for v in Rb])
    m.cfg.shift = 3
    for x in X[:10]:                                        # exports exactly
        assert m.infer(x.tolist()).logits == int_logits(R1, R2, x[None], 3, Rb)[0].tolist()


def test_calibrate_margin_is_the_smallest_meeting_the_precision():
    # Gap g with correctness: wrong at gaps 0..9, right at 10 and above.
    gaps = np.arange(40)
    logits = np.zeros((40, 8), dtype=int)
    logits[:, 1] = gaps                                     # class 1 wins by `gap`
    y = np.where(gaps >= 10, 1, 2)
    margin, _ = calibrate(logits, y, np.full(40, 256), precision=0.99)
    assert margin == 10
    margin, _ = calibrate(logits, y, np.full(40, 256), precision=0.75)
    assert margin <= 10 and np.mean((y == 1)[gaps >= margin]) >= 0.75


def test_dataset_is_balanced_and_split_by_capture():
    ds = build(4, seed=5, classes=["UART", "SPI", "I2C"])
    assert set(ds.y) == {0, 1, 2}
    assert all(0 <= v <= 63 for row in ds.X for v in row)
    assert len(set(ds.group)) <= 12


@pytest.mark.parametrize("seed", [3])
def test_small_training_run_beats_chance_and_exports(tmp_path, seed):
    classes = ["UART", "SPI", "I2C", "PS/2"]
    tc = TrainConfig(float_epochs=300, qat_epochs=300, restarts=1)
    model, report = train(captures_per_class=12, seed=seed, classes=classes, tc=tc)
    assert report.test_accuracy > 0.5                       # chance is 0.25
    assert sum(map(sum, report.confusion)) > 0
    save(model, report, tmp_path / "w")
    back = load(tmp_path / "w")
    assert back.weights() == model.weights() and back.bias == model.bias
    assert back.cfg == model.cfg
    assert len((tmp_path / "w.hex").read_text().split()) == 11      # 168 bytes, 16 per line
