"""Bit-exact model of the input path in src/project.v: two-flop synchroniser, per-pin glitch
filter (bypass, 2-of-3 or 3-of-5 majority) and edge detector.

Timing convention (matches the RTL cycle by cycle): u[n] is the pin level during cycle n,
i.e. the value sampled at the clock edge that ends cycle n. The RTL computes filt_d one
cycle ahead and registers it, which gives the same values as the combinational form here:
    sync2[n] = u[n-2]            hist_k[n] = u[n-3-k]   (k = 0..4)
    filt_d[n] = u[n-2]                          (bypass)
              = majority(u[n-3], u[n-4], u[n-5])  (2-of-3)
              = 3-of-5 of u[n-3] .. u[n-7]        (3-of-5)
    strobe[n] = filt_d[n-1] xor filt_d[n-2]     (one cycle per filtered edge; milestone 0 put it on uo_out)
A clean edge at cycle e is therefore seen by filt_d at e + 2, e + 4 or e + 5 depending on the
mode. Pure Python, no dependencies.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

BYPASS, MAJ_2OF3, MAJ_3OF5 = 0, 1, 2
LATENCY = {BYPASS: 2, MAJ_2OF3: 4, MAJ_3OF5: 5}      # clean edge to filt_d change
_TAPS = {BYPASS: (2,), MAJ_2OF3: (3, 4, 5), MAJ_3OF5: (3, 4, 5, 6, 7)}


def _filt(mode: int, u) -> int:
    if mode == BYPASS:
        return u(2)
    taps = _TAPS[mode]
    ones = sum(u(k) for k in taps)
    return int(ones * 2 > len(taps))


def filter_cycles(u: Sequence[int], mode: int, before: int = 0) -> Tuple[List[int], List[int]]:
    """Cycle-by-cycle reference. u[n] for n >= 0; u[n] = `before` for n < 0 (the pipeline
    holds that level at the start). Returns (filt_d[n], strobe[n]) for each n."""
    get = lambda n: u[n] if n >= 0 else before
    filt = [_filt(mode, lambda k, n=n: get(n - k)) for n in range(len(u))]
    fd = lambda n: filt[n] if n >= 0 else before
    strobe = [fd(n - 1) ^ fd(n - 2) for n in range(len(u))]
    return filt, strobe


def filter_edges(initial: int, edges: Sequence[Tuple[int, int]], mode: int) -> List[Tuple[int, int]]:
    """Event-driven equivalent for one pin: pad changes (cycle, level) in, filtered changes
    (cycle at which filt_d takes the new level, level) out. The pad holds `initial` before
    its first change and the pipeline is settled at that level."""
    times = [t for t, _ in edges]
    levels = [v for _, v in edges]

    def u(n: int) -> int:
        # binary search for the last change at or before n
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= n:
                lo = mid + 1
            else:
                hi = mid
        return levels[lo - 1] if lo else initial

    cands = sorted({t + k for t in times for k in _TAPS[mode]})
    out: List[Tuple[int, int]] = []
    cur = initial
    for n in cands:
        v = _filt(mode, lambda k: u(n - k))
        if v != cur:
            out.append((n, v))
            cur = v
    return out


def filter_pins(initial: Sequence[int], edges: Sequence[Tuple[int, int, int]], mode: int
                ) -> List[Tuple[int, int, int]]:
    """filter_edges for several pins: (cycle, pin, level) in and out, sorted by cycle."""
    per: Dict[int, List[Tuple[int, int]]] = {p: [] for p in range(len(initial))}
    for t, p, v in edges:
        per[p].append((t, v))
    out = []
    for p in per:
        out += [(t, p, v) for t, v in filter_edges(initial[p], per[p], mode)]
    return sorted(out)
