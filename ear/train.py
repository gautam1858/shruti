"""Training for the Ear's ternary classifier (numpy), export to the 1,280-bit weight chain,
and evaluation of the exact integer model.

Training is quantisation-aware: the forward pass uses ternary weights (threshold 0.7 x mean
|w| per layer, as in ternary weight networks) with a straight-through gradient to the float
weights, and a threshold per hidden unit. It starts from a float network trained the same
way. After training, the requantisation shift is chosen on the validation set using the exact
integer model from ear.mlp, and every reported number comes from that integer model, not from
the float network.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import protosim as ps
from .dataset import MIN_EDGES, Dataset, build
from .mlp import CLASSES_N, HIDDEN, Config, Model

N_IN = 72


def ternarize(w: np.ndarray) -> np.ndarray:
    delta = 0.7 * np.mean(np.abs(w))
    return (np.sign(w) * (np.abs(w) > delta)).astype(np.int64)


def split(ds: Dataset, seed: int, frac=(0.7, 0.15, 0.15)):
    """Split by capture, so windows of one capture never land in two sets."""
    rng = np.random.default_rng(seed)
    groups = np.array(sorted(set(ds.group)))
    rng.shuffle(groups)
    n = len(groups)
    a, b = int(frac[0] * n), int((frac[0] + frac[1]) * n)
    parts = [set(groups[:a]), set(groups[a:b]), set(groups[b:])]
    X, y, g = np.array(ds.X, dtype=np.int64), np.array(ds.y), np.array(ds.group)
    e = np.array(ds.edges)
    return [(X[np.isin(g, list(p))], y[np.isin(g, list(p))], e[np.isin(g, list(p))])
            for p in parts]


@dataclass
class TrainConfig:
    float_epochs: int = 2000
    qat_epochs: int = 4000
    lr: float = 0.005
    restarts: int = 4
    seed: int = 0


def _scaled(w: np.ndarray) -> Tuple[np.ndarray, float]:
    """Ternary weights times one positive scale per layer. A per-layer scale leaves the
    integer model's argmax unchanged, so it is dropped at export (the thresholds are
    converted with it)."""
    t = ternarize(w)
    nz = np.abs(w)[t != 0]
    alpha = float(nz.mean()) if nz.size else 1.0
    return alpha * t, alpha


def _train_float(X: np.ndarray, y: np.ndarray, classes: Sequence[int], tc: TrainConfig,
                 seed: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Float pre-training, then quantisation-aware training with ternary weights and a
    straight-through gradient. Hidden units have a threshold (bias); the output layer has
    none. Returns ternary W1, W2 and the integer thresholds."""
    rng = np.random.default_rng(seed)
    x = X / 255.0
    W = [rng.normal(0, 0.3, (HIDDEN, N_IN)), rng.normal(0, 0.3, (CLASSES_N, HIDDEN)),
         np.zeros(HIDDEN)]
    counts = np.bincount(y, minlength=CLASSES_N).astype(float)
    cw = np.where(counts > 0, counts.sum() / np.maximum(counts, 1) / len(classes), 0.0)
    sw = cw[y]
    onehot = np.eye(CLASSES_N)[y]
    mask = np.full(CLASSES_N, -1e9)
    mask[list(classes)] = 0.0                      # untrained outputs never win in training
    M = [np.zeros_like(w) for w in W]
    V = [np.zeros_like(w) for w in W]
    b1, b2, eps = 0.9, 0.999, 1e-8
    total = tc.float_epochs + tc.qat_epochs
    alpha1 = 1.0
    for t in range(1, total + 1):
        qat = t > tc.float_epochs
        if qat:
            (W1, alpha1), (W2, _) = _scaled(W[0]), _scaled(W[1])
        else:
            W1, W2 = W[0], W[1]
        a1 = x @ W1.T + W[2]
        h = np.maximum(a1, 0)
        z = h @ W2.T + mask
        z = z - z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        gz = (p - onehot) * sw[:, None] / len(y)
        ga1 = (gz @ W2) * (a1 > 0)
        G = [ga1.T @ x, gz.T @ h, ga1.sum(axis=0)]
        lr = tc.lr * (0.3 if qat else 1.0) * (0.1 + 0.9 * (1 - (t - 1) / total))
        for i in range(3):
            M[i] = b1 * M[i] + (1 - b1) * G[i]
            V[i] = b2 * V[i] + (1 - b2) * G[i] ** 2
            W[i] -= lr * (M[i] / (1 - b1 ** t)) / (np.sqrt(V[i] / (1 - b2 ** t)) + eps)
        if qat:                                    # keep thresholds inside 8 signed bits
            lim = 127 * alpha1 / 255
            W[2] = np.clip(W[2], -lim, lim)
    T1, T2 = ternarize(W[0]), ternarize(W[1])
    T2[[k for k in range(CLASSES_N) if k not in classes]] = 0   # untrained outputs stay 0
    _, alpha1 = _scaled(W[0])
    bias = np.clip(np.round(W[2] * 255 / alpha1), -128, 127).astype(np.int64)
    return T1, T2, bias


def int_logits(T1: np.ndarray, T2: np.ndarray, X: np.ndarray, shift: int,
               bias: Optional[np.ndarray] = None) -> np.ndarray:
    """The exact integer model of ear.mlp, vectorised (checked against it in the tests)."""
    acc = X @ T1.T + (0 if bias is None else bias)
    h = np.minimum(255, np.maximum(acc, 0) >> shift)
    return h @ T2.T


def predict(logits: np.ndarray) -> np.ndarray:
    return np.argmax(logits, axis=1)               # numpy picks the lowest index on ties


def _best_shift(T1, T2, bias, X, y) -> Tuple[int, float]:
    scores = [(np.mean(predict(int_logits(T1, T2, X, s, bias)) == y), -s, s) for s in range(16)]
    acc, _, s = max(scores)
    return s, float(acc)


def calibrate(logits: np.ndarray, y: np.ndarray, edges: np.ndarray,
              false_alarm: float = 0.05) -> Tuple[int, int]:
    """MARGIN: the median margin of correct predictions (so about half of them are marked
    confident, the other half not, on validation data). FLOOR: the false_alarm quantile of
    the best logit on known classes."""
    srt = np.sort(logits, axis=1)
    best, second = srt[:, -1], srt[:, -2]
    correct = predict(logits) == y
    margin = int(np.median((best - second)[correct])) if correct.any() else 0
    floor = int(np.floor(np.quantile(best, false_alarm)))
    return max(0, min(255, margin)), max(-2048, min(2047, floor))


@dataclass
class Report:
    classes: List[str]
    trained_classes: List[str]
    windows: Dict[str, int]
    shift: int
    margin: int
    floor: int
    min_edges: int
    val_accuracy: float
    test_accuracy: float
    confusion: List[List[int]]                     # rows: true class, columns: predicted
    confident_rate: float
    confident_accuracy: float
    ood_known: float                               # OOD flag rate on trained classes
    ood_unknown: Optional[float] = None            # OOD flag rate on held-out classes


def train(captures_per_class: int = 60, seed: int = 1, classes: Sequence[str] = ps.CLASSES,
          tc: TrainConfig = TrainConfig(), unknown: Sequence[str] = ()) -> Tuple[Model, Report]:
    ds = build(captures_per_class, seed, classes)
    (Xtr, ytr, _), (Xva, yva, eva), (Xte, yte, ete) = split(ds, seed)
    cls_idx = [ps.CLASSES.index(c) for c in classes]
    best = None
    for r in range(tc.restarts):
        T1, T2, bias = _train_float(Xtr, ytr, cls_idx, tc, tc.seed + r)
        s, acc = _best_shift(T1, T2, bias, Xva, yva)
        if best is None or acc > best[0]:
            best = (acc, T1, T2, bias, s)
    val_acc, T1, T2, bias, shift = best
    margin, floor = calibrate(int_logits(T1, T2, Xva, shift, bias), yva, eva)
    model = Model(T1.tolist(), T2.tolist(), Config(shift, margin, floor, MIN_EDGES),
                  [int(b) for b in bias])

    lt = int_logits(T1, T2, Xte, shift, bias)
    pred = predict(lt)
    conf = np.zeros((CLASSES_N, CLASSES_N), dtype=int)
    for t, p in zip(yte, pred):
        conf[t, p] += 1
    srt = np.sort(lt, axis=1)
    confident = (srt[:, -1] - srt[:, -2]) >= margin
    ood = (srt[:, -1] < floor) | (ete < MIN_EDGES)
    ood_unknown = None
    if unknown:
        du = build(max(4, captures_per_class // 3), seed + 1000, unknown)
        lu = int_logits(T1, T2, np.array(du.X, dtype=np.int64), shift, bias)
        ood_unknown = float(np.mean(np.max(lu, axis=1) < floor))
    report = Report(
        classes=list(ps.CLASSES), trained_classes=list(classes),
        windows={c: int(np.sum(np.array(ds.y) == ps.CLASSES.index(c))) for c in classes},
        shift=shift, margin=margin, floor=floor, min_edges=MIN_EDGES,
        val_accuracy=round(val_acc, 4), test_accuracy=round(float(np.mean(pred == yte)), 4),
        confusion=conf.tolist(), confident_rate=round(float(np.mean(confident)), 4),
        confident_accuracy=round(float(np.mean((pred == yte)[confident])) if confident.any() else 0.0, 4),
        ood_known=round(float(np.mean(ood)), 4),
        ood_unknown=None if ood_unknown is None else round(ood_unknown, 4))
    return model, report


def save(model: Model, report: Report, stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.with_suffix(".hex").write_text(model.to_hex())
    stem.with_suffix(".json").write_text(json.dumps(
        {"config": asdict(model.cfg), "report": asdict(report)}, indent=2) + "\n")


def load(stem: Path) -> Model:
    meta = json.loads(stem.with_suffix(".json").read_text())
    return Model.from_hex(stem.with_suffix(".hex").read_text(), Config(**meta["config"]))


def report_markdown(r: Report, title: str) -> str:
    names = r.classes
    lines = [f"## {title}", "",
             f"Trained on: {', '.join(r.trained_classes)}. Windows per class: "
             + ", ".join(f"{k} {v}" for k, v in r.windows.items()) + ".", "",
             f"- Integer model accuracy: validation {r.val_accuracy:.1%}, test {r.test_accuracy:.1%}",
             f"- Configuration: SHIFT {r.shift}, MARGIN {r.margin}, FLOOR {r.floor}, "
             f"MIN_EDGES {r.min_edges}",
             f"- Confident on {r.confident_rate:.1%} of test windows; accuracy when confident "
             f"{r.confident_accuracy:.1%}",
             f"- OOD flag rate on trained classes (false alarms): {r.ood_known:.1%}"]
    if r.ood_unknown is not None:
        lines.append(f"- OOD flag rate on classes held out of training: {r.ood_unknown:.1%}")
    rows = [i for i, n in enumerate(names) if n in r.trained_classes]
    lines += ["", "Confusion matrix on the test set (rows: true class, columns: predicted):", "",
              "| | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for i in rows:
        lines.append(f"| {names[i]} | " + " | ".join(str(v) for v in r.confusion[i]) + " |")
    return "\n".join(lines) + "\n"
