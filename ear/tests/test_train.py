"""Training pipeline: small, fast runs (the full run is `python -m shruti train`)."""

import numpy as np
import pytest

from ear import protosim as ps
from ear.dataset import build
from ear.mlp import Model
from ear.train import (TrainConfig, _class_weights, _fit_tau, _xent, calibrate, calibrate_classes,
                       int_logits,
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
    # Foreign windows with gaps 0..19: at most 5% may be confident, so MARGIN rises to 19.
    foreign = np.zeros((20, 8), dtype=int)
    foreign[:, 3] = np.arange(20)
    margin, _ = calibrate(logits, y, np.full(40, 256), precision=0.99, foreign=foreign)
    assert margin == 19 and np.mean(np.arange(20) >= margin) <= 0.05


def test_per_class_margins_meet_each_precision_and_share_the_foreign_budget():
    # Class 1 is right only from a gap of 10; class 4 is always right. Twenty foreign
    # windows look like class 1 with gaps 0..19, and at most 5% (one) may be confident.
    gaps = np.arange(40)
    logits = np.zeros((80, 8), dtype=int)
    logits[:40, 1] = gaps
    logits[40:, 4] = gaps
    y = np.concatenate([np.where(gaps >= 10, 1, 2), np.full(40, 4)])
    foreign = np.zeros((20, 8), dtype=int)
    foreign[:, 1] = np.arange(20)
    m = calibrate_classes(logits, y, 0.99, foreign, 0.05)
    assert m[1] == 19 and m[4] == 0                  # one global margin would be 19 for both
    global_m, _ = calibrate(logits, y, np.full(80, 256), precision=0.99, foreign=foreign)
    assert global_m == 19
    # No margins can meet a budget of zero foreign windows when one has a gap of 255.
    foreign[0, 1] = 300
    assert calibrate_classes(logits, y, 0.99, foreign[:10], 0.05) == [255] * 8


def test_auto_prescale_picks_the_smallest_tick_that_fills_a_window():
    from ear.dataset import auto_windows
    fast, info_f = auto_windows("SPI", "t/spi", half=6)               # 4 MHz SCK
    slow, info_s = auto_windows("UART", "t/uart", period=50_000_000 / 9600)
    assert info_f["prescale"] == 1 and fast and all(w.edges >= 256 for w in fast)
    assert info_s["prescale"] > 1 and slow
    assert max(w.edges for w in slow) > 24                         # past the activity gate


def test_dataset_is_balanced_and_split_by_capture():
    ds = build(4, seed=5, classes=["UART", "SPI", "I2C"])
    assert set(ds.y) == {0, 1, 2}
    assert all(0 <= v <= 63 for row in ds.X for v in row)
    assert len(set(ds.group)) <= 12


@pytest.mark.parametrize("seed", [3])
def test_small_training_run_beats_chance_and_exports(tmp_path, seed):
    classes = ["UART", "SPI", "I2C", "PS/2"]
    tc = TrainConfig(float_epochs=300, qat_epochs=300, restarts=1, foreign=6)
    model, report = train(captures_per_class=12, seed=seed, classes=classes, tc=tc)
    assert report.test_accuracy > 0.5                       # chance is 0.25
    assert sum(map(sum, report.confusion)) > 0
    save(model, report, tmp_path / "w")
    back = load(tmp_path / "w")
    assert back.weights() == model.weights() and back.bias == model.bias
    assert back.cfg == model.cfg
    assert len((tmp_path / "w.hex").read_text().split()) == 11      # 168 bytes, 16 per line
