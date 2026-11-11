"""Cycle-exact reference of Watch (watch/SPEC.md): 2 rule monitors, the trigger and a
16-entry flight recorder. Pure Python, no dependencies.

`Watch.step` is one clock cycle. It takes what the RTL sees in that cycle (the filtered pins
and their previous value, the timestamp, the Ear's OOD flag and class pins, the trigger-in pad,
and any host write that lands in the cycle) and returns the cycle's combinational outputs; the
registers then take their next values, as at the clock edge. Host writes in a cycle win over
the cycle's own updates, as in the RTL, where they come last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ear.features import log2_code

PINS = 12
MONITORS = 2
ENTRIES = 16
ALL_PINS = (1 << PINS) - 1

CTRL, SRC, POST, CAUSE, COUNT, TTIME, PINSEL, DATA, HITS, MON = (
    0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE8, 0xEA, 0xEC, 0xF0)

SRC_MON0, SRC_MON1, SRC_OOD, SRC_CLASS, SRC_EXT = 1, 2, 4, 8, 16
EDGE_OFF, EDGE_RISE, EDGE_FALL, EDGE_ANY = 0, 1, 2, 3


@dataclass
class Monitor:
    pin: int = 0
    edge: int = EDGE_OFF
    act_sync: bool = False
    syncidx: int = 0
    mask: int = 0
    value: int = 0
    cmin: int = 0
    cmax: int = 63
    since: int = 0xFFFF          # cycles since the pin's previous edge, saturating
    hits: int = 0

    def cfg_byte(self) -> int:
        return (self.pin & 15) | self.edge << 4 | self.act_sync << 7

    def matches(self, vis: int, edges: int) -> bool:
        if self.edge == EDGE_OFF or self.pin >= PINS or not (edges >> self.pin) & 1:
            return False
        level = (vis >> self.pin) & 1
        if self.edge == EDGE_RISE and not level or self.edge == EDGE_FALL and level:
            return False
        if vis & self.mask != self.value & self.mask:
            return False
        return self.cmin <= log2_code(self.since) <= self.cmax


@dataclass
class Out:
    match: List[bool]            # each monitor's match this cycle
    fire: bool                   # the trigger fires this cycle
    record: bool                 # an entry is written this cycle
    sync_set: int                # SYNC flags to set for the EMs next cycle


@dataclass
class Watch:
    mons: List[Monitor] = field(default_factory=lambda: [Monitor() for _ in range(MONITORS)])
    armed: bool = False
    triggered: bool = False
    frozen: bool = False
    src: int = 0
    post: int = 0
    left: int = 0                # entries still to keep after the trigger
    cause: int = 0
    ttime: int = 0
    pins: int = ALL_PINS
    mem: List[int] = field(default_factory=lambda: [0] * ENTRIES)
    wptr: int = 0
    count: int = 0
    rbyte: int = 0               # DATA port position, 0..63
    # synchronisers and previous values
    tin1: int = 0
    tin2: int = 0
    tin3: int = 0
    ood_prev: int = 0
    shown_prev: int = 0

    # -- outputs ---------------------------------------------------------------------------

    @property
    def trig_out(self) -> int:
        return int(self.triggered)

    @property
    def irq(self) -> int:
        return int(self.frozen)

    # -- host ------------------------------------------------------------------------------

    def read(self, addr: int) -> int:
        """A register read with no side effect (DATA's advance is `take`)."""
        if addr == CTRL:
            return self.frozen << 2 | self.triggered << 1 | self.armed
        if addr == SRC:
            return self.src
        if addr == POST:
            return self.post
        if addr == CAUSE:
            return self.cause
        if addr == COUNT:
            return self.count
        if TTIME <= addr < TTIME + 3:
            return (self.ttime >> 8 * (addr - TTIME)) & 0xFF
        if addr == PINSEL:
            return self.pins & 0xFF
        if addr == PINSEL + 1:
            return self.pins >> 8
        if addr == DATA:
            return self.data_byte(self.rbyte)
        if addr in (HITS, HITS + 1):
            return self.mons[addr - HITS].hits
        if MON <= addr < MON + 8 * MONITORS:
            m, r = self.mons[(addr - MON) // 8], (addr - MON) % 8
            return [m.cfg_byte(), m.syncidx, m.mask & 0xFF, m.mask >> 8, m.value & 0xFF,
                    m.value >> 8, m.cmin, m.cmax][r]
        return 0

    def data_byte(self, b: int) -> int:
        k, j = b >> 2, b & 3
        if k >= self.count:
            return 0
        e = self.mem[(self.wptr - self.count + k) % ENTRIES]
        return [e & 0xFF, (e >> 8) & 0xFF, (e >> 16) & 0xFF, (e >> 24) & 0x0F][j]

    def entries(self) -> List[Tuple[int, int]]:
        """(levels, timestamp low 16 bits) of the valid entries, oldest first."""
        out = []
        for k in range(self.count):
            e = self.mem[(self.wptr - self.count + k) % ENTRIES]
            out.append((e >> 16, e & 0xFFFF))
        return out

    def _write(self, addr: int, d: int) -> None:
        if addr == CTRL:
            if d & 2:                          # TRIG: a trigger command, nothing else
                return
            self.triggered = self.frozen = False
            self.armed = bool(d & 1)
            if d & 1:
                self.count = self.wptr = self.cause = self.rbyte = 0
        elif addr == SRC:
            self.src = d & 0x1F
        elif addr == POST:
            self.post = d & 15
        elif addr == PINSEL:
            self.pins = (self.pins & 0xF00) | d
        elif addr == PINSEL + 1:
            self.pins = (self.pins & 0xFF) | (d & 15) << 8
        elif addr == DATA:
            self.rbyte = 0
        elif addr in (HITS, HITS + 1):
            self.mons[addr - HITS].hits = 0
        elif MON <= addr < MON + 8 * MONITORS:
            m, r = self.mons[(addr - MON) // 8], (addr - MON) % 8
            if r == 0:
                m.pin, m.edge = d & 15, (d >> 4) & 3
                m.act_sync = bool(d >> 7 & 1)
                m.since = 0xFFFF
            elif r == 1:
                m.syncidx = d & 3
            elif r == 2:
                m.mask = (m.mask & 0xF00) | d
            elif r == 3:
                m.mask = (m.mask & 0xFF) | (d & 15) << 8
            elif r == 4:
                m.value = (m.value & 0xF00) | d
            elif r == 5:
                m.value = (m.value & 0xFF) | (d & 15) << 8
            elif r == 6:
                m.cmin = d & 63
            else:
                m.cmax = d & 63

    # -- one cycle -------------------------------------------------------------------------

    def step(self, vis: int, vis_prev: int, cnt: int, ear_ood: int = 0, ear_shown: int = 0,
             trig_pad: int = 0, write: Optional[Tuple[int, int]] = None,
             take: bool = False) -> Out:
        """One cycle. `write` is a host write landing in this cycle, `take` a DATA read."""
        edges = (vis ^ vis_prev) & ALL_PINS
        match = [m.matches(vis, edges) for m in self.mons]
        sources = (match[0] * SRC_MON0 | match[1] * SRC_MON1
                   | (ear_ood and not self.ood_prev) * SRC_OOD
                   | (ear_shown != self.shown_prev) * SRC_CLASS
                   | (self.tin2 and not self.tin3) * SRC_EXT)
        host_trig = write is not None and write[0] == CTRL and write[1] & 2
        fired = sources & self.src
        fire = self.armed and not self.triggered and bool(fired or host_trig)
        record = self.armed and not self.frozen and bool(edges & self.pins)
        sync_set = 0
        for m, hit in zip(self.mons, match):
            if hit and m.act_sync:
                sync_set |= 1 << m.syncidx

        # registers
        if record:
            self.mem[self.wptr] = vis << 16 | (cnt & 0xFFFF)
            self.wptr = (self.wptr + 1) % ENTRIES
            self.count = min(ENTRIES, self.count + 1)
        if fire:
            self.triggered, self.cause, self.ttime = True, fired, cnt & 0xFFFFFF
            if self.post == 0:
                self.frozen = True
            else:
                self.left = self.post
        elif self.triggered and not self.frozen and record:
            self.left -= 1
            if self.left == 0:
                self.frozen = True
        for m, hit in zip(self.mons, match):
            if hit:
                m.hits = min(255, m.hits + 1)
            pin_edge = m.pin < PINS and (edges >> m.pin) & 1
            m.since = 1 if pin_edge else min(0xFFFF, m.since + 1)
        if take:
            self.rbyte = (self.rbyte + 1) % (4 * ENTRIES)
        self.tin3, self.tin2, self.tin1 = self.tin2, self.tin1, trig_pad & 1
        self.ood_prev, self.shown_prev = ear_ood & 1, ear_shown & 7
        if write is not None:
            self._write(*write)
        return Out(match, fire, record, sync_set)


def run(watch: Watch, levels: Sequence[int], start_cnt: int = 0, initial: int = 0,
        **per_cycle) -> List[Out]:
    """Step through a list of per-cycle pin levels (and optional per-cycle lists of the other
    inputs, by keyword), from timestamp `start_cnt`."""
    outs, prev = [], initial
    for k, v in enumerate(levels):
        kw = {name: seq[k] for name, seq in per_cycle.items()}
        outs.append(watch.step(v, prev, start_cnt + k, **kw))
        prev = v
    return outs
