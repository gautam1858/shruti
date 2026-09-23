"""Training data for the Ear: simulated captures -> input path -> feature windows.

Each capture is one bus from ear.protosim with random speed and options, a little timing
jitter, and sometimes glitches. It is mapped onto the 4 monitored pins in a random order,
filtered with a glitch-filter mode the bus can survive, and cut into windows exactly as the
hardware would. Windows with fewer than `min_edges` edges are dropped (the hardware's
activity gate reports them as out-of-distribution). Pure Python.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import protosim as ps
from .features import extract
from .inpath import BYPASS, MAJ_2OF3, MAJ_3OF5, filter_pins

MIN_EDGES = 24


@dataclass
class Dataset:
    X: List[List[int]] = field(default_factory=list)     # 72 features per window
    y: List[int] = field(default_factory=list)           # class index
    group: List[int] = field(default_factory=list)       # capture index, for splitting
    edges: List[int] = field(default_factory=list)
    meta: List[dict] = field(default_factory=list)       # per capture

    def extend(self, other: "Dataset") -> None:
        off = len(self.meta)
        self.X += other.X
        self.y += other.y
        self.group += [g + off for g in other.group]
        self.edges += other.edges
        self.meta += other.meta


def filter_mode_for(cap: ps.Capture, rng: random.Random) -> int:
    """A filter mode that keeps the bus's real pulses: 3-of-5 drops pulses up to 2 cycles,
    2-of-3 up to 1 cycle."""
    shortest = cap.min_pulse()
    modes = [BYPASS]
    if shortest >= 3:
        modes.append(MAJ_2OF3)
    if shortest >= 6:
        modes.append(MAJ_3OF5)
    return rng.choice(modes)


def _truncate(cap: ps.Capture, max_edges: int) -> ps.Capture:
    """Keep the first max_edges edges (fast buses need far fewer cycles for a few windows)."""
    if len(cap.edges) <= max_edges:
        return cap
    edges = cap.edges[:max_edges]
    return ps.Capture(cap.label, cap.lines, cap.initial, edges, edges[-1][0] + 1,
                      cap.payload, cap.params)


def capture_windows(cap: ps.Capture, rng: random.Random, min_edges: int = MIN_EDGES,
                    jitter: Optional[int] = None, glitches: Optional[float] = None,
                    max_windows: int = 6):
    cap = _truncate(cap, 256 * (max_windows + 1))
    j = rng.choice([0, 0, 1, 2]) if jitter is None else jitter
    g = rng.choice([0.0, 0.0, 0.0, 0.5]) if glitches is None else glitches
    cap = ps.perturb(cap, rng, jitter=j, glitches=g)
    mode = filter_mode_for(cap, rng)
    assign = ps.random_assignment(cap, rng)
    idle = [rng.randrange(2) for _ in range(4)]
    init, edges = ps.to_pins(cap, assign, idle)
    windows = extract(init, filter_pins(init, edges, mode), cap.duration)
    kept = [w for w in windows if w.edges >= min_edges][:max_windows]
    info = dict(label=cap.label, params=cap.params, mode=mode, assign=assign,
                windows=len(windows), kept=len(kept))
    return kept, info


def build(captures_per_class: int, seed: int, classes: Sequence[str] = tuple(ps.CLASSES),
          min_edges: int = MIN_EDGES) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()
    for label in classes:
        k = ps.CLASSES.index(label)
        kw = {"duration": 1_500_000} if label == "UART" else {}   # slow baud rates need time
        for _ in range(captures_per_class):
            cap = ps.generate(label, rng, **kw)
            windows, info = capture_windows(cap, rng, min_edges)
            gid = len(ds.meta)
            ds.meta.append(info)
            for w in windows:
                ds.X.append(w.features)
                ds.y.append(k)
                ds.group.append(gid)
                ds.edges.append(w.edges)
    return ds
