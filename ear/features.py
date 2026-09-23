"""Bit-exact reference of the Ear's feature extractor (the definition the RTL must match).

Input: filtered edges on the 4 monitored pins, as (cycle, pin, level) from ear.inpath, and the
pins' filtered levels before the first edge. Output: one 72-feature snapshot (8-bit values)
per window. The definition, including the choices the architecture spec left open, is in
ear/SPEC.md; this module is its executable form.

Per pin p (12 features, 4 pins = 48):
  hist[0..7]  6-bit saturating counts of intervals binned by their ratio to the shortest
              interval seen so far in the window, in half-octave steps (1x, 1.4x, 2x, ... 11x+).
              When a new shortest interval arrives, the histogram shifts up by the number of
              half-octaves the minimum moved, merging into bin 7.
  min_code    6-bit log2 code of the shortest interval (63 if the pin had no interval)
  max_code    6-bit code of the longest interval within 11x of the shortest (0 if none)
  edges       8-bit saturating edge count
  high        floor(256 * cycles_high / window_length), at most 255
Per ordered pair (a, b), a != b, in the order (0,1) (0,2) (0,3) (1,0) (1,2) ... (3,2):
  near[a][b]  6-bit saturating count of b edges no more than 4 cycles after the latest a edge
  hi[a][b]    6-bit saturating count of b edges while a is high
Feature order: pin 0's 12, pin 1's 12, pin 2's 12, pin 3's 12, the 12 near counts, the 12 hi
counts.

A window closes at the end of the cycle in which the edge count on the monitored pins reaches
256, or after 65,536 cycles, whichever comes first. The classifier then runs for
INFER_CYCLES cycles, during which edges are not counted, and the next window starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import groupby
from typing import List, Optional, Sequence, Tuple

PINS = 4
PAIRS = [(a, b) for a in range(PINS) for b in range(PINS) if a != b]
N_FEATURES = PINS * 12 + 2 * len(PAIRS)            # 72
MAX_EDGES = 256
MAX_CYCLES = 65_536
NEAR = 4
INFER_CYCLES = 72 * 8 + 8 * 8 + 8                  # one weight per cycle, then argmax: 648

CODE_NONE_MIN, CODE_NONE_MAX = 63, 0


def log2_code(interval: int) -> int:
    """6-bit code: leading-one position (0..15) and the next two bits. Intervals saturate at
    65,535 cycles."""
    i = min(max(interval, 1), 0xFFFF)
    m = i.bit_length() - 1
    return m * 4 + (((i << 2) >> m) & 3)


def _sat(v: int, bits: int) -> int:
    return min(v, (1 << bits) - 1)


@dataclass
class Window:
    start: int
    end: int                                       # last cycle, inclusive
    features: List[int]
    edges: int                                     # edges counted (all monitored pins)

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass
class _PinState:
    last: Optional[int] = None                     # cycle of the latest edge in this window
    hist: List[int] = field(default_factory=lambda: [0] * 8)
    min_h: Optional[int] = None                    # half-octave index of the shortest interval
    min_code: int = CODE_NONE_MIN
    max_code: int = CODE_NONE_MAX
    edges: int = 0
    high: int = 0

    def interval(self, t: int) -> None:
        if self.last is None:
            return
        code = log2_code(t - self.last)
        h = code >> 1
        if self.min_h is None:
            self.min_h, self.min_code, self.max_code = h, code, code
            self.hist[0] = 1
            return
        if h < self.min_h:
            shift = self.min_h - h
            new = [0] * 8
            for k, c in enumerate(self.hist):
                j = min(7, k + shift)
                new[j] = _sat(new[j] + c, 6)
            self.hist = new
            self.min_h = h
            if (self.max_code >> 1) - h > 6:
                self.max_code = code               # the old longest is no longer in-burst
        d = h - self.min_h
        self.hist[min(7, d)] = _sat(self.hist[min(7, d)] + 1, 6)
        self.min_code = min(self.min_code, code)
        if d <= 6 and code > self.max_code:
            self.max_code = code


class Extractor:
    def __init__(self, initial: Sequence[int], start: int = 0,
                 max_edges: int = MAX_EDGES, max_cycles: int = MAX_CYCLES,
                 infer_cycles: int = INFER_CYCLES):
        self.level = list(initial)
        self.max_edges, self.max_cycles, self.infer = max_edges, max_cycles, infer_cycles
        self.windows: List[Window] = []
        self._open(start)

    def _open(self, w0: int) -> None:
        self.w0 = w0
        self.pins = [_PinState() for _ in range(PINS)]
        self.near = {pr: 0 for pr in PAIRS}
        self.hi = {pr: 0 for pr in PAIRS}
        self.n_edges = 0
        self.hc_from = w0                          # levels apply from this cycle on

    def _accumulate_high(self, upto: int) -> None:
        """Add cycles hc_from .. upto-1 at the current levels."""
        span = upto - self.hc_from
        if span > 0:
            for p in range(PINS):
                self.pins[p].high += self.level[p] * span
        self.hc_from = max(self.hc_from, upto)

    def _close(self, end: int) -> None:
        self._accumulate_high(end + 1)
        length = end - self.w0 + 1
        f: List[int] = []
        for s in self.pins:
            f += list(s.hist)
            f += [s.min_code, s.max_code, _sat(s.edges, 8), min(255, (s.high * 256) // length)]
        f += [self.near[pr] for pr in PAIRS]
        f += [self.hi[pr] for pr in PAIRS]
        self.windows.append(Window(self.w0, end, f, self.n_edges))
        self._open(end + 1 + self.infer)

    def _close_by_time_before(self, c: int) -> None:
        while c > self.w0 + self.max_cycles - 1 and c >= self.w0:
            self._close(self.w0 + self.max_cycles - 1)

    def feed(self, edges: Sequence[Tuple[int, int, int]]) -> None:
        """Process edges (sorted by cycle)."""
        for c, group in groupby(edges, key=lambda e: e[0]):
            group = list(group)
            self._close_by_time_before(c)
            if c < self.w0:                        # inference pause: track levels only
                for _, p, v in group:
                    self.level[p] = v
                continue
            self._accumulate_high(c)
            changed = [p for _, p, _ in group]
            for _, p, v in group:
                self.level[p] = v
                s = self.pins[p]
                s.interval(c)
                s.last = c
                s.edges += 1
            for b in changed:
                for a in range(PINS):
                    if a == b:
                        continue
                    la = self.pins[a].last
                    if la is not None and c - la <= NEAR:
                        self.near[(a, b)] = _sat(self.near[(a, b)] + 1, 6)
                    if self.level[a]:
                        self.hi[(a, b)] = _sat(self.hi[(a, b)] + 1, 6)
            self.n_edges += len(group)
            if self.n_edges >= self.max_edges:
                self._close(c)

    def finish(self, duration: int) -> List[Window]:
        """Close every window that ends by cycle `duration` - 1; drop a partial last one."""
        self._close_by_time_before(duration)
        return self.windows


def extract(initial: Sequence[int], edges: Sequence[Tuple[int, int, int]], duration: int,
            **kw) -> List[Window]:
    """Feature windows for filtered edges on the 4 monitored pins over [0, duration)."""
    ex = Extractor(initial, **kw)
    ex.feed(edges)
    return ex.finish(duration)
