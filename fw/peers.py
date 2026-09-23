"""Python models of the devices the firmware talks to.

Reactive peers (SPI slave, I2C slave) are iss.Device subclasses: they watch the pads every
evaluated cycle and answer by scheduling external level changes, one cycle after the edge that
caused them at the earliest. The UART peers are a stimulus generator (transmitter) and a
trace decoder (receiver).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from iss import Device

# -- UART ---------------------------------------------------------------------------------


def uart_stimulus(pin: int, data: Sequence[int], period: float, start: int = 10,
                  gap: float = 0.0, stop_bits: Optional[Sequence[int]] = None
                  ) -> List[Tuple[int, int, int]]:
    """Edges of 8N1 frames on `pin` at a (possibly fractional) bit period. `gap` is idle time
    between frames in bit periods. `stop_bits` optionally overrides each frame's stop level."""
    edges: List[Tuple[int, int, int]] = []
    t, level = float(start), 1
    for k, byte in enumerate(data):
        stop = 1 if stop_bits is None else stop_bits[k]
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [stop]
        for i, v in enumerate(bits):
            if v != level:
                edges.append((round(t + i * period), pin, v))
                level = v
        t += (10 + gap) * period
        if level == 0:                        # line must idle high between frames
            edges.append((round(t - gap * period), pin, 1))
            level = 1
    return edges


def uart_decode(trace: Sequence[Tuple[int, int]], initial: int, period: int,
                end: int) -> List[Tuple[int, int, bool]]:
    """Receive 8N1 frames from a pin's (cycle, level) change list, sampling mid-bit.
    Returns (start cycle, byte, stop bit ok)."""
    def level_at(c: int) -> int:
        lvl = initial
        for t, v in trace:
            if t > c:
                break
            lvl = v
        return lvl

    frames = []
    i, n = 0, len(trace)
    while i < n:
        t, v = trace[i]
        if v == 0 and level_at(t - 1) == 1 and t + 10 * period <= end + period:
            byte = sum(level_at(t + period // 2 + (k + 1) * period) << k for k in range(8))
            stop = level_at(t + period // 2 + 9 * period) == 1
            frames.append((t, byte, stop))
            limit = t + 9 * period + period // 2
            while i < n and trace[i][0] <= limit:
                i += 1
            continue
        i += 1
    return frames


# -- SPI ----------------------------------------------------------------------------------


@dataclass
class SpiSlave(Device):
    """Mode-0 SPI slave: samples MOSI on SCK rising, changes MISO after SCK falling."""

    sck: int
    mosi: int
    miso: int
    cs: int
    responses: List[int] = field(default_factory=list)
    received: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    rise_times: List[int] = field(default_factory=list)

    def attach(self, sim) -> None:
        super().attach(sim)
        self.prev = {p: sim.pad[p] for p in (self.sck, self.mosi, self.cs)}
        self.mosi_changed = -1
        self.selected = False

    def on_cycle(self, c: int) -> None:
        pad = self.sim.pad
        sck, mosi, cs = pad[self.sck], pad[self.mosi], pad[self.cs]
        if mosi != self.prev[self.mosi]:
            self.mosi_changed = c
        if self.prev[self.cs] == 1 and cs == 0:
            self.selected, self.bit, self.inb = True, 0, 0
            self.outb = self.responses.pop(0) if self.responses else 0xFF
            self.sim.set_external(self.miso, (self.outb >> 7) & 1, c + 1)
            if sck != 0:
                self.errors.append(f"{c}: CS fell with SCK high")
        elif self.selected and self.prev[self.sck] == 0 and sck == 1:
            if self.mosi_changed == c:
                self.errors.append(f"{c}: MOSI changed on the sampling edge")
            self.inb = (self.inb << 1) | mosi
            self.bit += 1
            self.rise_times.append(c)
        elif self.selected and self.prev[self.sck] == 1 and sck == 0:
            if self.bit < 8:
                self.sim.set_external(self.miso, (self.outb >> (7 - self.bit)) & 1, c + 1)
        if self.prev[self.cs] == 0 and cs == 1 and self.selected:
            self.selected = False
            if self.bit != 8:
                self.errors.append(f"{c}: CS rose after {self.bit} clocks")
            self.received.append(self.inb)
        self.prev = {self.sck: sck, self.mosi: mosi, self.cs: cs}


# -- I2C ----------------------------------------------------------------------------------


@dataclass
class I2cSlave(Device):
    """I2C slave on open-drain SCL/SDA. ACKs its address and data bytes unless told to NACK,
    and can stretch the clock after each ACK. Records every transaction and protocol events."""

    scl: int
    sda: int
    address: int = 0x50
    nack_at: Optional[int] = None         # byte index (0 = address) to NACK
    stretch: int = 0                      # cycles to hold SCL low after each ACK clock
    transactions: List[List[int]] = field(default_factory=list)
    events: List[Tuple[int, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def attach(self, sim) -> None:
        super().attach(sim)
        self.prev_scl, self.prev_sda = sim.pad[self.scl], sim.pad[self.sda]
        self.active = False
        self.cur: List[int] = []
        self.bit, self.shift, self.acking, self.addressed = 0, 0, False, False

    def on_cycle(self, c: int) -> None:
        scl, sda = self.sim.pad[self.scl], self.sim.pad[self.sda]
        ps, pd = self.prev_scl, self.prev_sda
        if ps == 1 and scl == 1 and pd != sda:
            if sda == 0:
                self.events.append((c, "START"))
                if self.active:
                    self.errors.append(f"{c}: repeated START")
                self.active, self.cur, self.bit, self.shift = True, [], 0, 0
                self.addressed = False
            else:
                self.events.append((c, "STOP"))
                if self.active:
                    self.transactions.append(self.cur)
                self.active = False
        elif self.active and ps == 0 and scl == 1:
            if self.bit < 8:
                self.shift = (self.shift << 1) | sda
            self.bit += 1
        elif self.active and ps == 1 and scl == 0:
            if self.bit == 8:
                byte = self.shift & 0xFF
                self.cur.append(byte)
                idx = len(self.cur) - 1
                if idx == 0:
                    self.addressed = (byte >> 1) == self.address and not (byte & 1)
                ack = self.addressed and idx != self.nack_at
                if ack:
                    self.sim.set_external(self.sda, 0, c + 1)
                    self.acking = True
                self.events.append((c, "ACK" if ack else "NACK"))
            elif self.bit == 9:
                if self.acking:
                    self.sim.set_external(self.sda, 1, c + 1)
                    self.acking = False
                if self.stretch:
                    self.sim.set_external(self.scl, 0, c + 1)
                    self.sim.set_external(self.scl, 1, c + 1 + self.stretch)
                    self.events.append((c, "STRETCH"))
                self.bit, self.shift = 0, 0
        self.prev_scl, self.prev_sda = scl, sda


def i2c_timing(trace: Sequence[Tuple[int, int, int]], scl: int, sda: int):
    """Measure I2C timing from the pad trace: SCL low and high widths, START hold, STOP setup
    and bus-free time. Returns a dict of minimum values (cycles)."""
    edges = sorted((c, p, v) for (c, p, v) in trace if p in (scl, sda))
    lvl = {scl: 1, sda: 1}
    last = {(scl, 0): None, (scl, 1): None, (sda, 0): None, (sda, 1): None}
    low, high, hold, setup, buf = [], [], [], [], []
    last_start = last_stop = None
    for c, p, v in edges:
        if p == scl:
            if v == 1 and last[(scl, 0)] is not None:
                low.append(c - last[(scl, 0)])
            if v == 0 and last[(scl, 1)] is not None:
                high.append(c - last[(scl, 1)])
            if v == 0 and last_start is not None:
                hold.append(c - last_start)
                last_start = None
        else:
            if lvl[scl] == 1 and v == 0:
                last_start = c
                if last_stop is not None:
                    buf.append(c - last_stop)
            if lvl[scl] == 1 and v == 1:
                last_stop = c
                if last[(scl, 1)] is not None:
                    setup.append(c - last[(scl, 1)])
        lvl[p] = v
        last[(p, v)] = c
    m = lambda xs: min(xs) if xs else None
    return {"low": m(low), "high": m(high), "start_hold": m(hold), "stop_setup": m(setup),
            "bus_free": m(buf)}


def spi_master_stimulus(sck: int, mosi: int, cs: int, data: Sequence[int], half: int,
                        setup: int, start: int = 20, gap: int = 50):
    """Mode-0 SPI master as stimulus: CS falls with MOSI bit 7, SCK rises `setup` cycles
    later, each SCK phase lasts `half` cycles, MOSI changes on falling edges. Returns the
    edges and, per byte, the rising-edge cycles (where the master samples MISO)."""
    edges: List[Tuple[int, int, int]] = []
    rises: List[List[int]] = []
    t = start
    mosi_level = 1
    for byte in data:
        edges.append((t, cs, 0))
        r = []
        bits = [(byte >> (7 - k)) & 1 for k in range(8)]
        if bits[0] != mosi_level:
            edges.append((t, mosi, bits[0]))
            mosi_level = bits[0]
        rise = t + setup
        for k in range(8):
            edges.append((rise, sck, 1))
            r.append(rise)
            fall = rise + half
            edges.append((fall, sck, 0))
            if k < 7 and bits[k + 1] != mosi_level:
                edges.append((fall, mosi, bits[k + 1]))
                mosi_level = bits[k + 1]
            rise = fall + half
        t = rise
        edges.append((t, cs, 1))
        rises.append(r)
        t += gap
    return edges, rises


class Wire(Device):
    """Copies pad levels from one pin to another one cycle later (board wiring)."""

    def __init__(self, pairs: Sequence[Tuple[int, int]]):
        self.pairs = list(pairs)

    def attach(self, sim) -> None:
        super().attach(sim)
        self.prev = {a: sim.pad[a] for a, _ in self.pairs}
        for a, b in self.pairs:
            sim.ext[b] = sim.pad[a]
            sim.pad[b] = sim.pad[a]

    def on_cycle(self, c: int) -> None:
        for a, b in self.pairs:
            v = self.sim.pad[a]
            if v != self.prev[a]:
                self.sim.set_external(b, v, c + 1)
                self.prev[a] = v
