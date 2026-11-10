"""Training for the Ear's ternary classifier (numpy), export to the 1,344-bit weight chain,
and evaluation of the exact integer model.

The recipe, per restart:
1. Quantisation-aware training: the forward pass uses ternary weights (threshold 0.7 x mean
   |w| per layer, as in ternary weight networks) with a straight-through gradient to the
   float weights, and a threshold per hidden unit. It starts from a float network trained the
   same way.
2. The requantisation shift is chosen on the validation set with the exact integer model.
3. Exact search (`refine`): the integer model itself, as ear.mlp defines it, is improved one
   weight or threshold at a time, keeping a change only if it lowers the class-balanced
   cross-entropy of the integer logits on the training set. Gradient training stalls well
   short of what the ternary network can fit (84-90% on its own training data); the search
   optimises the model the hardware runs, so nothing is lost at export.
The restart with the best balanced validation accuracy (mean recall over classes) is kept.
MARGIN is the smallest margin at which the confident windows are at least 99% right on the
validation set and at most 5% of foreign windows (random edges and clocks, not any trained
protocol; ear.dataset.build_foreign) are confident. Without the second condition the model
is confident on most foreign traffic. Every reported number comes from the integer model,
never from the float network.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import protosim as ps
from .dataset import MIN_EDGES, Dataset, build, build_foreign
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
    sweeps: int = 60                    # exact-search sweeps per restart (0: no search)
    precision: float = 0.99             # MARGIN target: precision of confident windows
    foreign: int = 200                  # foreign captures for MARGIN (0: none), half held out
    foreign_rate: float = 0.05          # MARGIN target: confident share of foreign windows
    per_class_margins: bool = True      # also calibrate one margin per class (EAR_MARGIN_K)


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


def _class_weights(y: np.ndarray) -> np.ndarray:
    counts = np.bincount(y, minlength=CLASSES_N).astype(float)
    present = counts > 0
    w = np.where(present, counts.sum() / np.maximum(counts, 1) / present.sum(), 0.0)
    return w[y]


def _xent(Z: np.ndarray, y: np.ndarray, tau: float, sw: np.ndarray) -> float:
    """Class-balanced cross-entropy of the integer logits Z scaled by tau."""
    a = tau * Z
    m = a.max(axis=1)
    lse = m + np.log(np.exp(a - m[:, None]).sum(axis=1))
    return float(np.sum((lse - a[np.arange(len(y)), y]) * sw) / len(y))


def _fit_tau(Z: np.ndarray, y: np.ndarray, sw: np.ndarray) -> float:
    """The logit scale that minimises the loss (it does not change any prediction)."""
    taus = np.exp(np.linspace(np.log(1e-3), np.log(2.0), 40))
    return float(min(taus, key=lambda t: _xent(Z, y, t, sw)))


def refine(T1: np.ndarray, T2: np.ndarray, bias: np.ndarray, shift: int, X: np.ndarray,
           y: np.ndarray, sweeps: int = 60, seed: int = 0
           ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Exact search on the integer model: in each sweep, visit every weight (moved to each
    other ternary value) and every threshold (moved by 1, 2, 4 or 8, within 8 signed bits)
    in a random order, and keep a move if it lowers the class-balanced cross-entropy of the
    integer logits on (X, y). The logit scale is refitted after each sweep. Stops when a
    sweep keeps nothing. The training loss never rises, and the result is a valid model."""
    rng = np.random.default_rng(seed)
    T1, T2, b = T1.astype(np.int64).copy(), T2.astype(np.int64).copy(), bias.astype(np.int64).copy()
    X = X.astype(np.int64)
    sw = _class_weights(y)
    A = X @ T1.T + b                               # accumulators, then hidden units, logits
    H = np.minimum(255, np.maximum(A, 0) >> shift)
    Z = H @ T2.T
    tau = _fit_tau(Z, y, sw)
    L = _xent(Z, y, tau, sw)
    moves = ([(0, j, i) for j in range(HIDDEN) for i in range(N_IN)]
             + [(1, k, j) for k in range(CLASSES_N) for j in range(HIDDEN)]
             + [(2, j, d) for j in range(HIDDEN) for d in (-8, -4, -2, -1, 1, 2, 4, 8)])

    def hidden_move(j, Aj):
        Hj = np.minimum(255, np.maximum(Aj, 0) >> shift)
        dH = Hj - H[:, j]
        if not dH.any():
            return None
        Z2 = Z + np.outer(dH, T2[:, j])
        return Hj, Z2, _xent(Z2, y, tau, sw)

    for _ in range(sweeps):
        kept = 0
        for n in rng.permutation(len(moves)):
            kind, p, q = moves[n]
            if kind == 0:                          # W1[p][q]
                old = T1[p, q]
                for new in (-1, 0, 1):
                    if new == old:
                        continue
                    Aj = A[:, p] + (new - old) * X[:, q]
                    r = hidden_move(p, Aj)
                    if r and r[2] < L - 1e-12:
                        T1[p, q] = new
                        A[:, p], H[:, p], Z, L = Aj, r[0], r[1], r[2]
                        kept += 1
                        break
            elif kind == 1:                        # W2[p][q]
                old = T2[p, q]
                for new in (-1, 0, 1):
                    if new == old:
                        continue
                    Z2 = Z.copy()
                    Z2[:, p] += (new - old) * H[:, q]
                    L2 = _xent(Z2, y, tau, sw)
                    if L2 < L - 1e-12:
                        T2[p, q] = new
                        Z, L = Z2, L2
                        kept += 1
                        break
            else:                                  # threshold B[p] += q
                nb = int(np.clip(b[p] + q, -128, 127))
                if nb == b[p]:
                    continue
                Aj = A[:, p] + (nb - b[p])
                r = hidden_move(p, Aj)
                if r and r[2] < L - 1e-12:
                    b[p] = nb
                    A[:, p], H[:, p], Z, L = Aj, r[0], r[1], r[2]
                    kept += 1
        tau = _fit_tau(Z, y, sw)
        L = _xent(Z, y, tau, sw)
        if kept == 0:
            break
    return T1, T2, b


def balanced_accuracy(pred: np.ndarray, y: np.ndarray) -> float:
    """Mean recall over the classes present in y."""
    return float(np.mean([np.mean(pred[y == k] == k) for k in np.unique(y)]))


def _best_shift(T1, T2, bias, X, y) -> Tuple[int, float]:
    scores = [(np.mean(predict(int_logits(T1, T2, X, s, bias)) == y), -s, s) for s in range(16)]
    acc, _, s = max(scores)
    return s, float(acc)


def margins(logits: np.ndarray) -> np.ndarray:
    srt = np.sort(logits, axis=1)
    return srt[:, -1] - srt[:, -2]


def calibrate(logits: np.ndarray, y: np.ndarray, edges: np.ndarray,
              false_alarm: float = 0.05, precision: float = 0.99,
              foreign: Optional[np.ndarray] = None, foreign_rate: float = 0.05
              ) -> Tuple[int, int]:
    """MARGIN: the smallest margin (0..255) at which the windows marked confident are at
    least `precision` right on validation data and, given foreign logits, at most
    `foreign_rate` of the foreign windows are confident (255 if none is). FLOOR: the
    false_alarm quantile of the best logit on known classes."""
    srt = np.sort(logits, axis=1)
    best = srt[:, -1]
    gap = margins(logits)
    fgap = margins(foreign) if foreign is not None and len(foreign) else None
    correct = predict(logits) == y
    margin = 255
    for m in range(256):
        conf = gap >= m
        if (conf.any() and np.mean(correct[conf]) >= precision
                and (fgap is None or np.mean(fgap >= m) <= foreign_rate)):
            margin = m
            break
    floor = int(np.floor(np.quantile(best, false_alarm)))
    return margin, max(-2048, min(2047, floor))


def calibrate_classes(logits: np.ndarray, y: np.ndarray, precision: float = 0.99,
                      foreign: Optional[np.ndarray] = None, foreign_rate: float = 0.05
                      ) -> List[int]:
    """Per-class MARGIN (EAR_MARGIN_K): class k's margin is one at which the validation
    windows called k with at least that margin are at least `precision` right. Together the
    eight let at most `foreign_rate` of the foreign windows be confident; within that budget
    they maximise the mean over classes of the share of each class's windows that are
    confident and right (an exact search over classes, a knapsack on foreign windows). A
    class with no acceptable margin gets 255, the most a register holds, and so does every
    class if no choice meets the foreign budget."""
    gap, pred = margins(logits), predict(logits)
    fgap = margins(foreign) if foreign is not None and len(foreign) else np.zeros(0, np.int64)
    fpred = predict(foreign) if len(fgap) else np.zeros(0, np.int64)
    budget = int(np.floor(foreign_rate * len(fgap)))
    options = []                                   # per class: (margin, foreign cost, value)
    for k in range(CLASSES_N):
        n_k = max(1, int(np.sum(y == k)))
        opts = []
        for m in range(256):
            c = (pred == k) & (gap >= m)
            if c.any() and np.mean(y[c] == k) < precision:
                continue
            opts.append((m, int(np.sum((fpred == k) & (fgap >= m))),
                         float(np.sum(c & (y == k))) / n_k))
        options.append(opts or [(255, int(np.sum((fpred == k) & (fgap >= 255))), 0.0)])
    best = np.full((CLASSES_N + 1, budget + 1), -np.inf)
    best[0, :] = 0.0
    pick = np.zeros((CLASSES_N, budget + 1), dtype=np.int64)
    for k, opts in enumerate(options):
        for b in range(budget + 1):
            for i, (_, cost, value) in enumerate(opts):
                if cost <= b and best[k, b - cost] + value > best[k + 1, b]:
                    best[k + 1, b] = best[k, b - cost] + value
                    pick[k, b] = i
    out, b = [255] * CLASSES_N, int(np.argmax(best[CLASSES_N]))
    if not np.isfinite(best[CLASSES_N, b]):        # no margins meet the foreign budget:
        return out                                 # 255 for all, as calibrate() does
    for k in reversed(range(CLASSES_N)):
        m, cost, _ = options[k][pick[k, b]]
        out[k] = m
        b -= cost
    return out


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
    recall: Optional[Dict[str, float]] = None      # per trained class, on the test set
    balanced_accuracy: Optional[float] = None      # mean of recall, on the test set
    foreign_confident: Optional[float] = None      # confident share of held-out foreign windows
    margins: Optional[List[int]] = None            # per-class MARGIN (EAR_MARGIN_K), if used


def train(captures_per_class: int = 60, seed: int = 1, classes: Sequence[str] = ps.CLASSES,
          tc: TrainConfig = TrainConfig(), unknown: Sequence[str] = (),
          ds: Optional[Dataset] = None) -> Tuple[Model, Report]:
    """Train on build(captures_per_class, seed, classes), or on `ds` if given (it must be
    that same dataset; ear.evaluate passes it in to reuse the split)."""
    ds = ds if ds is not None else build(captures_per_class, seed, classes)
    (Xtr, ytr, _), (Xva, yva, eva), (Xte, yte, ete) = split(ds, seed)
    cls_idx = [ps.CLASSES.index(c) for c in classes]
    best = None
    for r in range(tc.restarts):
        T1, T2, bias = _train_float(Xtr, ytr, cls_idx, tc, tc.seed + r)
        s, _ = _best_shift(T1, T2, bias, Xva, yva)
        if tc.sweeps:
            T1, T2, bias = refine(T1, T2, bias, s, Xtr, ytr, tc.sweeps, tc.seed + r)
        pv = predict(int_logits(T1, T2, Xva, s, bias))
        bal = balanced_accuracy(pv, yva)
        if best is None or bal > best[0]:
            best = (bal, float(np.mean(pv == yva)), T1, T2, bias, s)
    _, val_acc, T1, T2, bias, shift = best
    Fcal = Fte = None
    if tc.foreign:                                 # half sets MARGIN, half is held out
        fds = build_foreign(tc.foreign, seed + 2000)
        Xf, gf = np.array(fds.X, dtype=np.int64).reshape(-1, 72), np.array(fds.group)
        Fcal = int_logits(T1, T2, Xf[gf % 2 == 0], shift, bias)
        Fte = int_logits(T1, T2, Xf[gf % 2 == 1], shift, bias)
    Lva = int_logits(T1, T2, Xva, shift, bias)
    margin, floor = calibrate(Lva, yva, eva, precision=tc.precision, foreign=Fcal,
                              foreign_rate=tc.foreign_rate)
    per_class = (calibrate_classes(Lva, yva, tc.precision, Fcal, tc.foreign_rate)
                 if tc.per_class_margins else None)
    model = Model(T1.tolist(), T2.tolist(), Config(shift, margin, floor, MIN_EDGES, per_class),
                  [int(b) for b in bias])
    mvec = np.array(per_class if per_class else [margin] * CLASSES_N)

    lt = int_logits(T1, T2, Xte, shift, bias)
    pred = predict(lt)
    conf = np.zeros((CLASSES_N, CLASSES_N), dtype=int)
    for t, p in zip(yte, pred):
        conf[t, p] += 1
    srt = np.sort(lt, axis=1)
    confident = (srt[:, -1] - srt[:, -2]) >= mvec[pred]
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
        ood_unknown=None if ood_unknown is None else round(ood_unknown, 4),
        recall={c: round(float(np.mean(pred[yte == ps.CLASSES.index(c)]
                                       == ps.CLASSES.index(c))), 4)
                for c in classes if np.any(yte == ps.CLASSES.index(c))},
        balanced_accuracy=round(balanced_accuracy(pred, yte), 4),
        foreign_confident=None if Fte is None else round(float(np.mean(
            margins(Fte) >= mvec[predict(Fte)])), 4),
        margins=per_class)
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
             f"- Integer model accuracy: validation {r.val_accuracy:.1%}, test {r.test_accuracy:.1%}"
             + (f" (balanced: {r.balanced_accuracy:.1%})" if r.balanced_accuracy is not None
                else ""),
             f"- Configuration: SHIFT {r.shift}, MARGIN {r.margin}, FLOOR {r.floor}, "
             f"MIN_EDGES {r.min_edges}",
             (f"- Per-class MARGIN (EAR_MARGIN_K): "
              + ", ".join(f"{names[k]} {m}" for k, m in enumerate(r.margins))
              + ". Each class is at least 99% right among its confident validation windows, "
              "and together at most 5% of foreign validation windows are confident; MARGIN "
              "alone (one margin for every class, for a host that writes only EAR_MARGIN) is "
              "the smallest giving the same on all classes at once"
              if r.margins else
              "- MARGIN is the smallest margin giving 99% on validation with at most 5% of "
              "foreign validation windows confident"),
             f"- Confident on {r.confident_rate:.1%} of test windows; accuracy when confident "
             f"{r.confident_accuracy:.1%}",
             f"- OOD flag rate on trained classes (false alarms): {r.ood_known:.1%}"]
    if r.ood_unknown is not None:
        lines.append(f"- OOD flag rate on classes held out of training: {r.ood_unknown:.1%}")
    if r.foreign_confident is not None:
        lines.append(f"- Confident on {r.foreign_confident:.1%} of held-out foreign windows "
                     "(random edges and clocks)")
    if r.recall:
        lines.append("- Recall per class (test): "
                     + ", ".join(f"{c} {v:.0%}" for c, v in r.recall.items()))
    rows = [i for i, n in enumerate(names) if n in r.trained_classes]
    lines += ["", "Confusion matrix on the test set (rows: true class, columns: predicted):", "",
              "| | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for i in rows:
        lines.append(f"| {names[i]} | " + " | ".join(str(v) for v in r.confusion[i]) + " |")
    return "\n".join(lines) + "\n"
