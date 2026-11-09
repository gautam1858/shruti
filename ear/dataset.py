"""Training data for the Ear: simulated captures -> input path -> feature windows.

Each capture is one bus from ear.protosim with random speed and options, a little timing
jitter, and sometimes glitches. It is mapped onto the 4 monitored pins in a random order,
filtered with a glitch-filter mode the bus can survive, and cut into windows exactly as the
hardware would. Windows with fewer than `min_edges` edges are dropped (the hardware's
activity gate reports them as out-of-distribution). Pure Python.

With the prescaler (EAR_CFG), each capture is cut at the prescaler a host would pick
(`auto_prescale`): the smallest of 1, 4, 16 and 64 at which the first window closes on its
edge count rather than on time, 64 if none does. A slow bus needs longer captures at a
bigger prescaler; generators are deterministic per seed, so the capture is regenerated
longer with the same seed, which gives the same bus with more traffic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from . import protosim as ps
from .features import MAX_EDGES, PRESCALES, extract
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
                    max_windows: int = 6, prescale: int = 1):
    cap = _truncate(cap, 256 * (max_windows + 1))
    j = rng.choice([0, 0, 1, 2]) if jitter is None else jitter
    g = rng.choice([0.0, 0.0, 0.0, 0.5]) if glitches is None else glitches
    cap = ps.perturb(cap, rng, jitter=j, glitches=g)
    mode = filter_mode_for(cap, rng)
    assign = ps.random_assignment(cap, rng)
    idle = [rng.randrange(2) for _ in range(4)]
    init, edges = ps.to_pins(cap, assign, idle)
    windows = extract(init, filter_pins(init, edges, mode), cap.duration, prescale=prescale)
    kept = [w for w in windows if w.edges >= min_edges][:max_windows]
    info = dict(label=cap.label, params=cap.params, mode=mode, assign=assign,
                windows=len(windows), kept=len(kept), prescale=prescale)
    return kept, info


def auto_prescale(initial: Sequence[int], edges, duration: int) -> Optional[int]:
    """The prescaler a host would pick for filtered edges on the 4 pins: the smallest P whose
    first window closes on its edge count (256 edges) rather than on time; 64 if none does.
    None if the capture is too short to hold a complete window at a prescaler it needs."""
    for P in PRESCALES:
        ws = extract(initial, edges, duration, prescale=P)
        if not ws:
            return None
        if ws[0].edges >= MAX_EDGES:
            return P
    return PRESCALES[-1]


def auto_windows(label: str, key: str, min_edges: int = MIN_EDGES, max_windows: int = 6,
                 base: Optional[int] = None, **gen_kw):
    """Generate a capture of `label` (seeded by `key`) and cut it at the prescaler a host
    would pick, as auto_prescale does: try 1, 4, 16, 64 in turn, each on a capture base x P
    cycles long, and stop at the first whose first window closes on its edge count."""
    base = base or (1_500_000 if label == "UART" else 400_000)
    for P in PRESCALES:
        cap = ps.generate(label, random.Random(f"{key}/bus"), duration=base * P, **gen_kw)
        ws, info = capture_windows(cap, random.Random(f"{key}/view"), min_edges=0,
                                   max_windows=max_windows, prescale=P)
        if ws and (ws[0].edges >= MAX_EDGES or P == PRESCALES[-1]):
            kept = [w for w in ws if w.edges >= min_edges][:max_windows]
            info.update(kept=len(kept))
            return kept, info
    return [], dict(label=label, params={}, prescale=None, windows=0, kept=0)


# -- signals that are not any of the trained protocols -------------------------------------

Edges = List[Tuple[int, int, int]]


def toggles(times: Sequence[int], pin: int) -> Edges:
    out, lvl = [], 0
    for t in times:
        lvl ^= 1
        out.append((t, pin, lvl))
    return out


def random_edges(rng: random.Random, mean: float, pins: int, duration: int) -> Edges:
    """Each of `pins` pins toggles at exponentially distributed intervals of mean `mean`."""
    ev: Edges = []
    for p in range(pins):
        t, ts = 0.0, []
        while True:
            t += rng.expovariate(1 / mean)
            if t >= duration:
                break
            ts.append(int(t))
        ev += toggles(sorted(set(ts)), p)
    return sorted(ev)


def clock(freq: float, duration: int, pin: int = 0) -> Edges:
    half = ps.CLOCK_HZ / freq / 2
    return toggles([int(round(k * half)) for k in range(1, int(duration / half))], pin)


def foreign_signal(rng: random.Random, duration: int) -> Edges:
    """A non-protocol signal, the kind MARGIN is calibrated against: random edges on 1-4 pins
    (mean interval 20 cycles to 20,000), one clock, or two clocks (10 kHz to 10 MHz)."""
    kind = rng.choice(["random", "random", "clock", "clocks"])
    if kind == "random":
        return random_edges(rng, 10 ** rng.uniform(1.3, 4.3), rng.randint(1, 4), duration)
    a = clock(10 ** rng.uniform(4, 7), duration)
    if kind == "clock":
        return a
    return sorted(a + clock(10 ** rng.uniform(4, 7), duration, pin=1))


def auto_signal_windows(make: Callable[[random.Random, int], Edges], key: str,
                        min_edges: int = MIN_EDGES, max_windows: int = 6,
                        base: int = 1_500_000):
    """Windows of an edge-list signal (`make(rng, duration)`, deterministic per key) at the
    prescaler a host would pick, as auto_windows does for protocol captures. The unused
    pins idle at random levels; the filter is in bypass."""
    for P in PRESCALES:
        edges = make(random.Random(f"{key}/signal"), base * P)
        active = {p for _, p, _ in edges}
        rng = random.Random(f"{key}/idle")
        init = [0 if p in active else rng.randrange(2) for p in range(4)]
        ws = extract(init, edges, base * P, prescale=P)
        if ws and (ws[0].edges >= MAX_EDGES or P == PRESCALES[-1]):
            return [w for w in ws if w.edges >= min_edges][:max_windows], P
    return [], None


def build_foreign(captures: int, seed: int, min_edges: int = MIN_EDGES) -> Dataset:
    """Windows of `captures` foreign signals, each at the prescaler a host would pick. y is
    -1: these are not a class; they set how sure the Ear must be before it answers."""
    ds = Dataset()
    for i in range(captures):
        key = f"{seed}/foreign/{i}"
        windows, P = auto_signal_windows(lambda r, d: foreign_signal(r, d), key, min_edges)
        ds.meta.append(dict(label="foreign", prescale=P, kept=len(windows)))
        for w in windows:
            ds.X.append(w.features)
            ds.y.append(-1)
            ds.group.append(i)
            ds.edges.append(w.edges)
    return ds


def build(captures_per_class: int, seed: int, classes: Sequence[str] = tuple(ps.CLASSES),
          min_edges: int = MIN_EDGES, prescale="auto") -> Dataset:
    """Simulated captures cut into windows. prescale="auto" picks each capture's prescaler
    as a host would (auto_windows); prescale=1 is every capture at 1 cycle per tick (the
    dataset before the prescaler existed, reproduced exactly)."""
    rng = random.Random(seed)
    ds = Dataset()
    for label in classes:
        k = ps.CLASSES.index(label)
        kw = {"duration": 1_500_000} if label == "UART" else {}   # slow baud rates need time
        for i in range(captures_per_class):
            if prescale == "auto":
                windows, info = auto_windows(label, f"{seed}/{label}/{i}", min_edges)
            else:
                cap = ps.generate(label, rng, **kw)
                windows, info = capture_windows(cap, rng, min_edges, prescale=prescale)
            gid = len(ds.meta)
            ds.meta.append(info)
            for w in windows:
                ds.X.append(w.features)
                ds.y.append(k)
                ds.group.append(gid)
                ds.edges.append(w.edges)
    return ds
