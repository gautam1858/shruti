"""Python models of the devices the firmware talks to.

Reactive peers (SPI slave, I2C slave and master, open-drain wiring) are iss.Device subclasses:
they watch the pads every evaluated cycle and answer by scheduling external level changes, one
cycle after the edge that caused them at the earliest. The UART peers are a stimulus generator
(transmitter) and a trace decoder (receiver).
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
    """SPI slave in any mode (CPOL = mode >> 1, CPHA = mode & 1), MSB first. SCK idles at
    CPOL; the leading edge leaves the idle level. With CPHA = 0 it drives bit 7 when CS falls,
    samples MOSI on leading edges and shifts MISO after trailing edges; with CPHA = 1 it
    shifts MISO after leading edges and samples on trailing edges. MISO changes one cycle
    after the edge the slave sees."""

    sck: int
    mosi: int
    miso: int
    cs: int
    responses: List[int] = field(default_factory=list)
    received: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    sample_times: List[int] = field(default_factory=list)   # cycles of the sampling edges
    mode: int = 0

    def attach(self, sim) -> None:
        super().attach(sim)
        self.prev = {p: sim.pad[p] for p in (self.sck, self.mosi, self.cs)}
        self.mosi_changed = -1
        self.selected = False

    def on_cycle(self, c: int) -> None:
        pad = self.sim.pad
        cpol, cpha = self.mode >> 1, self.mode & 1
        sck, mosi, cs = pad[self.sck], pad[self.mosi], pad[self.cs]
        if mosi != self.prev[self.mosi]:
            self.mosi_changed = c
        leading = self.prev[self.sck] == cpol and sck != cpol
        trailing = self.prev[self.sck] != cpol and sck == cpol
        sample, shift = (leading, trailing) if cpha == 0 else (trailing, leading)
        if self.prev[self.cs] == 1 and cs == 0:
            self.selected, self.bit, self.inb = True, 0, 0
            self.outb = self.responses.pop(0) if self.responses else 0xFF
            if cpha == 0:
                self.sim.set_external(self.miso, (self.outb >> 7) & 1, c + 1)
            if sck != cpol:
                self.errors.append(f"{c}: CS fell with SCK not at its idle level")
        elif self.selected and sample:
            if self.mosi_changed == c:
                self.errors.append(f"{c}: MOSI changed on the sampling edge")
            self.inb = (self.inb << 1) | mosi
            self.bit += 1
            self.sample_times.append(c)
        elif self.selected and shift:
            if self.bit < 8:                  # present the next bit
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
    """Measure I2C timing from the pad trace: SCL low and high widths, SCL period (rise to
    rise), START hold, STOP setup, bus-free time and data setup (an SDA change while SCL is
    low to the next SCL rise). Returns a dict of minimum values (cycles)."""
    edges = sorted((c, p, v) for (c, p, v) in trace if p in (scl, sda))
    lvl = {scl: 1, sda: 1}
    last = {(scl, 0): None, (scl, 1): None, (sda, 0): None, (sda, 1): None}
    low, high, hold, setup, buf, period, dsetup = [], [], [], [], [], [], []
    sda_change = None
    last_start = last_stop = None
    for c, p, v in edges:
        if p == scl:
            if v == 1 and last[(scl, 0)] is not None:
                low.append(c - last[(scl, 0)])
            if v == 1 and last[(scl, 1)] is not None:
                period.append(c - last[(scl, 1)])
            if v == 1 and sda_change is not None:
                dsetup.append(c - sda_change)
                sda_change = None
            if v == 0 and last[(scl, 1)] is not None:
                high.append(c - last[(scl, 1)])
            if v == 0 and last_start is not None:
                hold.append(c - last_start)
                last_start = None
        else:
            if lvl[scl] == 0:
                sda_change = c
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
    return {"low": m(low), "high": m(high), "period": m(period), "start_hold": m(hold),
            "stop_setup": m(setup), "bus_free": m(buf), "data_setup": m(dsetup)}



def i2c_conditions(trace: Sequence[Tuple[int, int, int]], scl: int, sda: int) -> List[str]:
    """The START and STOP conditions on the bus, in order: every SDA change while SCL is high.
    A change of both lines in the same cycle counts SCL first."""
    level = {scl: 1, sda: 1}
    out: List[str] = []
    for c, p, v in sorted((c, p, v) for (c, p, v) in trace if p in (scl, sda)):
        if p == sda and level[scl] == 1 and v != level[sda]:
            out.append("START" if v == 0 else "STOP")
        level[p] = v
    return out


class I2cMaster(Device):
    """I2C master writer as a peer model, on open-drain SCL/SDA driven through the external
    levels, so a target pulling a line low wins. Sends each transaction (address byte, then
    data bytes), reads the ACK of each byte, stops a transaction at its first NACK, and ends
    with a STOP, or with a repeated START when `repeated` is set and another transaction
    follows. SCL is low for `low` cycles and high for `high` cycles; after releasing SCL it
    waits until the pad is high (clock stretching). SDA changes `hold` cycles after each SCL
    fall (0 is allowed: both lines change in the same cycle). `acks` records, per transaction,
    the SDA level of each ACK clock (0 = ACK)."""

    def __init__(self, scl: int, sda: int, transactions: Sequence[Sequence[int]], low: int,
                 high: int, hold: int = 8, start: int = 20, gap: int = 200,
                 repeated: bool = False):
        self.scl, self.sda = scl, sda
        self.transactions = [list(t) for t in transactions]
        self.low, self.high, self.hold = low, high, hold
        self.start, self.gap, self.repeated = start, gap, repeated
        self.acks: List[List[int]] = []
        self.done: Optional[int] = None

    def attach(self, sim) -> None:
        super().attach(sim)
        self.now = 0
        self._gen = self._run()
        self._wait = next(self._gen)

    def _drive(self, pin: int, level: int, at: int) -> None:
        self.sim.set_external(pin, level, at)

    def _scl_high(self) -> bool:
        return self.sim.pad[self.scl] == 1

    def _clock(self, bit: int):
        """One SCL clock from a high SCL: fall, SDA := bit after the hold, release, wait for
        the pad to rise, stay high. Leaves SCL high; returns the SDA level at the end."""
        fall = self.now + 1
        self._drive(self.scl, 0, fall)
        self._drive(self.sda, bit, fall + self.hold)
        yield fall + self.low - 1
        self._drive(self.scl, 1, self.now + 1)
        yield self._scl_high
        yield self.now + self.high - 1
        return self.sim.pad[self.sda]

    def _run(self):
        yield self.start
        n = len(self.transactions)
        for k, data in enumerate(self.transactions):
            self._drive(self.sda, 0, self.now + 1)            # START (SCL is high)
            yield self.now + self.high
            acks: List[int] = []
            for byte in data:
                for j in range(8):
                    yield from self._clock((byte >> (7 - j)) & 1)
                acks.append((yield from self._clock(1)))      # release SDA for the ACK
                if acks[-1]:
                    break
            self.acks.append(acks)
            if self.repeated and k + 1 < n:                   # repeated START next
                yield from self._clock(1)
                continue
            yield from self._clock(0)                         # STOP: SDA rises, SCL high
            self._drive(self.sda, 1, self.now + 1)
            yield self.now + self.gap
        self.done = self.now
        yield None

    def on_cycle(self, c: int) -> None:
        self.now = c
        while self._wait is not None:
            w = self._wait
            if callable(w):
                if not w():
                    return
            elif c < w:
                return
            self._wait = next(self._gen)

    def next_event(self, c: int) -> Optional[int]:
        w = self._wait
        return w if isinstance(w, int) and w > c else None


class OpenDrainBus(Device):
    """Board wiring for open-drain lines: each line joins several pins, and each pin sees,
    one cycle later, the wired-AND of what the other pins drive (low or released)."""

    def __init__(self, lines: Sequence[Sequence[int]]):
        self.lines = [list(line) for line in lines]

    def attach(self, sim) -> None:
        super().attach(sim)
        self.prev = {p: 1 for line in self.lines for p in line}

    def _pulls(self, p: int) -> bool:
        return bool(self.sim.oe[p]) and self.sim.out[p] == 0

    def on_cycle(self, c: int) -> None:
        for line in self.lines:
            for p in line:
                v = 0 if any(self._pulls(q) for q in line if q != p) else 1
                if v != self.prev[p]:
                    self.sim.set_external(p, v, c + 1)
                    self.prev[p] = v


def spi_master_stimulus(sck: int, mosi: int, cs: int, data: Sequence[int], half: int,
                        setup: int, start: int = 20, gap: int = 50, mode: int = 0):
    """SPI master as stimulus, in any mode (CPOL = mode >> 1, CPHA = mode & 1). SCK idles at
    CPOL. CS falls, the first leading edge comes `setup` cycles later and each SCK phase
    lasts `half` cycles. CPHA = 0: MOSI bit 7 goes out with CS and later bits on trailing
    edges, and the master samples on leading edges. CPHA = 1: each MOSI bit goes out on a
    leading edge and the master samples on trailing edges. Returns the edges and, per byte,
    the cycles at which the master samples MISO."""
    cpol, cpha = mode >> 1, mode & 1
    edges: List[Tuple[int, int, int]] = []
    samples: List[List[int]] = []
    t = start
    mosi_level = 1

    def put(c, bit):
        nonlocal mosi_level
        if bit != mosi_level:
            edges.append((c, mosi, bit))
            mosi_level = bit

    for byte in data:
        edges.append((t, cs, 0))
        r = []
        bits = [(byte >> (7 - k)) & 1 for k in range(8)]
        if cpha == 0:
            put(t, bits[0])
        lead = t + setup
        for k in range(8):
            edges.append((lead, sck, 1 - cpol))
            trail = lead + half
            edges.append((trail, sck, cpol))
            if cpha == 0:
                r.append(lead)
                if k < 7:
                    put(trail, bits[k + 1])
            else:
                put(lead, bits[k])
                r.append(trail)
            lead = trail + half
        t = lead
        edges.append((t, cs, 1))
        samples.append(r)
        t += gap
    return edges, samples


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
