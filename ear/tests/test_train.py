"""Training pipeline: small, fast runs (the full run is `python -m shruti train`)."""

import numpy as np
import pytest

from ear import protosim as ps
from ear.dataset import build
from ear.mlp import Model
from ear.train import TrainConfig, int_logits, load, save, train, ternarize


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
