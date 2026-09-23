"""Protocol simulator: edge streams for training and testing the Ear.

Each generator returns a Capture: the named lines of one bus, their initial levels, the edges
as (cycle, line, level) at the 50 MHz core clock, and the payload it encoded (so tests can
decode the stream and check it). Speeds, gaps, data and protocol options are drawn from the
caller's random generator; `perturb` adds timing jitter and short glitches afterwards.

Classes (the Ear's output order): UART, SPI, I2C, USB-LS, CAN, JTAG/SWD, PS/2, Manchester.
Pure Python, no dependencies.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

CLOCK_HZ = 50_000_000
CLASSES = ["UART", "SPI", "I2C", "USB-LS", "CAN", "JTAG/SWD", "PS/2", "Manchester"]


@dataclass
class Capture:
    label: str
    lines: List[str]
    initial: List[int]
    edges: List[Tuple[int, int, int]]          # (cycle, line index, level), sorted
    duration: int
    payload: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def min_pulse(self) -> int:
        """Shortest time a line holds a level (cycles)."""
        last: Dict[int, int] = {}
        best = self.duration
        for t, ln, _ in self.edges:
            if ln in last:
                best = min(best, t - last[ln])
            last[ln] = t
        return best


class _Builder:
    """Collects level changes per line; times may be fractional and are rounded at the end."""

    def __init__(self, lines: Sequence[str], initial: Sequence[int]):
        self.lines = list(lines)
        self.initial = list(initial)
        self.level = list(initial)
        self.ev: List[Tuple[float, int, int]] = []
        self.t_end = 0.0

    def set(self, t: float, line: int, v: int) -> None:
        if self.level[line] != v:
            self.ev.append((t, line, v))
            self.level[line] = v
        self.t_end = max(self.t_end, t)

    def capture(self, label: str, duration: float, payload: dict, params: dict) -> Capture:
        edges = sorted((int(round(t)), ln, v) for t, ln, v in self.ev)
        edges = _clean(edges, self.initial)
        return Capture(label, self.lines, self.initial, edges, int(math.ceil(duration)),
                       payload, params)


def _clean(edges, initial):
    """Drop changes that do not change the level (after rounding) and keep one change per
    line per cycle (the last)."""
    last_in_cycle: Dict[Tuple[int, int], int] = {}
    for i, (t, ln, v) in enumerate(edges):
        last_in_cycle[(t, ln)] = i
    level = list(initial)
    out = []
    for i, (t, ln, v) in enumerate(edges):
        if last_in_cycle[(t, ln)] != i or level[ln] == v:
            continue
        out.append((t, ln, v))
        level[ln] = v
    return out


def _bits_lsb(byte: int, n: int = 8) -> List[int]:
    return [(byte >> i) & 1 for i in range(n)]


def _bits_msb(byte: int, n: int = 8) -> List[int]:
    return [(byte >> (n - 1 - i)) & 1 for i in range(n)]


def _loguniform(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


# -- UART ---------------------------------------------------------------------------------

def uart(rng: random.Random, duration: int = 400_000, period: Optional[float] = None,
         duplex: Optional[bool] = None) -> Capture:
    B = period or _loguniform(rng, CLOCK_HZ / 3_000_000, CLOCK_HZ / 2400)
    duplex = rng.random() < 0.3 if duplex is None else duplex
    data_bits = rng.choice([8, 8, 8, 7])
    parity = rng.choice([None, None, "even", "odd"])
    stop = rng.choice([1, 1, 1, 2])
    lines = ["TX", "RX"] if duplex else ["TX"]
    b = _Builder(lines, [1] * len(lines))
    payload = {ln: [] for ln in lines}
    for li in range(len(lines)):
        t = rng.uniform(0, 20) * B
        b.level[li] = 1
        while True:
            burst = rng.randint(1, 16)
            for _ in range(burst):
                if t + (12 + stop) * B > duration:
                    break
                byte = rng.randrange(1 << data_bits)
                bits = [0] + _bits_lsb(byte, data_bits)
                if parity:
                    p = sum(bits[1:]) & 1
                    bits.append(p if parity == "even" else p ^ 1)
                bits += [1] * stop
                for i, v in enumerate(bits):
                    b.set(t + i * B, li, v)
                payload[lines[li]].append(byte)
                t += len(bits) * B + (rng.expovariate(1.0) * B if rng.random() < 0.3 else 0)
            t += rng.expovariate(1 / (50 * B))
            if t + (12 + stop) * B > duration:
                break
    return b.capture("UART", duration, payload,
                     dict(period=B, data_bits=data_bits, parity=parity, stop=stop))


# -- SPI ----------------------------------------------------------------------------------

def spi(rng: random.Random, duration: int = 400_000, half: Optional[float] = None,
        mode: Optional[int] = None) -> Capture:
    H = half or _loguniform(rng, 2, 250)
    mode = rng.randrange(4) if mode is None else mode
    cpol, cpha = mode >> 1, mode & 1
    SCK, MOSI, MISO, CS = range(4)
    b = _Builder(["SCK", "MOSI", "MISO", "CS"], [cpol, 1, 1, 1])
    mosi_bytes, miso_bytes = [], []
    t = rng.uniform(1, 20) * H
    while True:
        n = rng.randint(1, 8)
        if t + (n * 18 + 4) * H > duration:
            break
        b.set(t, CS, 0)
        lead = t + rng.uniform(1, 3) * H
        for _ in range(n):
            mo, mi = rng.randrange(256), rng.randrange(256)
            mosi_bytes.append(mo)
            miso_bytes.append(mi)
            mob, mib = _bits_msb(mo), _bits_msb(mi)
            for k in range(8):
                trail = lead + H
                if cpha == 0:
                    if k == 0:
                        b.set(lead - H / 2, MOSI, mob[0])
                        b.set(lead - H / 2, MISO, mib[0])
                    b.set(lead, SCK, 1 - cpol)
                    b.set(trail, SCK, cpol)
                    if k < 7:
                        b.set(trail + min(1, H / 4), MOSI, mob[k + 1])
                        b.set(trail + min(1, H / 4), MISO, mib[k + 1])
                else:
                    b.set(lead, SCK, 1 - cpol)
                    b.set(lead + min(1, H / 4), MOSI, mob[k])
                    b.set(lead + min(1, H / 4), MISO, mib[k])
                    b.set(trail, SCK, cpol)
                lead = trail + H
            lead += rng.choice([0, 0, H, 4 * H])      # gap between bytes inside one select
        t = lead + H
        b.set(t, CS, 1)
        b.set(t, MISO, 1)                            # released: pulled up
        t += rng.expovariate(1 / (30 * H)) + 2 * H
    return b.capture("SPI", duration, {"mosi": mosi_bytes, "miso": miso_bytes},
                     dict(half=H, mode=mode))


# -- I2C ----------------------------------------------------------------------------------

def i2c(rng: random.Random, duration: int = 400_000, half: Optional[float] = None,
        stretch: Optional[bool] = None) -> Capture:
    H = half or rng.choice([250.0, 62.5, 25.0]) * rng.uniform(0.9, 1.1)
    duty = rng.uniform(0.4, 0.6)                     # fraction of the period SCL is high
    lo, hi = 2 * H * (1 - duty), 2 * H * duty
    stretch = rng.random() < 0.3 if stretch is None else stretch
    SCL, SDA = 0, 1
    b = _Builder(["SCL", "SDA"], [1, 1])
    transactions = []
    t = rng.uniform(1, 10) * H
    hold = min(lo / 3, 15.0)
    while True:
        n = rng.randint(1, 6)
        if t + (n + 1) * 9 * (lo + hi) + 10 * H > duration:
            break
        b.set(t, SDA, 0)                             # START
        t += hi / 2
        b.set(t, SCL, 0)
        tx = []
        for i in range(n):
            byte = rng.randrange(256)
            tx.append(byte)
            ack = 0 if rng.random() < 0.95 else 1
            for k, v in enumerate(_bits_msb(byte) + [ack]):
                b.set(t + hold, SDA, v)
                t += lo
                b.set(t, SCL, 1)
                t += hi
                b.set(t, SCL, 0)
            if stretch and rng.random() < 0.5:        # slave holds SCL low after the ACK
                t += rng.uniform(1, 20) * H
        b.set(t + hold, SDA, 0)                       # STOP
        t += lo
        b.set(t, SCL, 1)
        t += hi / 2
        b.set(t, SDA, 1)
        transactions.append(tx)
        t += rng.expovariate(1 / (40 * H)) + 3 * H
    return b.capture("I2C", duration, {"transactions": transactions},
                     dict(half=H, duty=duty, stretch=stretch))


# -- low-speed USB ------------------------------------------------------------------------

USB_LS_BIT = CLOCK_HZ / 1_500_000


def _usb_stuff(bits: List[int]) -> List[int]:
    out, ones = [], 0
    for v in bits:
        out.append(v)
        ones = ones + 1 if v else 0
        if ones == 6:
            out.append(0)
            ones = 0
    return out


def usb_ls(rng: random.Random, duration: int = 400_000) -> Capture:
    """Low-speed USB on D+/D-: NRZI with bit stuffing, SYNC, PID, payload, SE0 EOPs, and
    keep-alive EOPs every millisecond. Idle is J (D- high)."""
    Bt = USB_LS_BIT * rng.uniform(0.985, 1.015)       # 1.5% clock tolerance
    DP, DM = 0, 1
    b = _Builder(["D+", "D-"], [0, 1])
    packets = []
    t = rng.uniform(5, 40) * Bt
    next_keepalive = 50_000.0
    state = 1                                         # 1 = J, 0 = K

    def drive(tt, st):
        b.set(tt, DP, 0 if st else 1)
        b.set(tt, DM, 1 if st else 0)

    while True:
        if t >= next_keepalive:
            b.set(t, DP, 0)
            b.set(t, DM, 0)                           # SE0 keep-alive EOP
            drive(t + 2 * Bt, 1)
            state = 1
            t += 3 * Bt
            next_keepalive += 50_000
            continue
        nbytes = rng.randint(0, 8)
        data = [rng.randrange(256) for _ in range(nbytes)]
        pid = rng.choice([0x1, 0x9, 0x5, 0x3, 0xB, 0x2, 0xA])
        raw = [0, 0, 0, 0, 0, 0, 0, 1]                 # SYNC
        raw += _bits_lsb(pid, 4) + _bits_lsb(pid ^ 0xF, 4)
        for byte in data + ([rng.randrange(256), rng.randrange(256)] if pid in (0x3, 0xB) else []):
            raw += _bits_lsb(byte)
        line = _usb_stuff(raw)
        if t + (len(line) + 4) * Bt > min(duration, next_keepalive):
            if next_keepalive < duration:
                t = max(t, next_keepalive)
                continue
            break
        for i, v in enumerate(line):
            if v == 0:
                state ^= 1
            drive(t + i * Bt, state)
        t += len(line) * Bt
        b.set(t, DP, 0)
        b.set(t, DM, 0)                               # EOP: SE0 for 2 bits, then J
        drive(t + 2 * Bt, 1)
        state = 1
        packets.append(raw)
        t += 3 * Bt + rng.expovariate(1 / (60 * Bt)) + 4 * Bt
    return b.capture("USB-LS", duration, {"packets": packets}, dict(bit=Bt))


# -- CAN ----------------------------------------------------------------------------------

def _can_crc(bits: List[int]) -> int:
    crc = 0
    for v in bits:
        nxt = v ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def _can_stuff(bits: List[int]) -> List[int]:
    out, run, last = [], 0, None
    for v in bits:
        out.append(v)
        run = run + 1 if v == last else 1
        last = v
        if run == 5:
            out.append(1 - v)
            last, run = 1 - v, 1
    return out


def can(rng: random.Random, duration: int = 400_000, period: Optional[float] = None) -> Capture:
    """CAN 2.0A frames on the receive line of a transceiver (dominant = 0)."""
    B = period or rng.choice([400.0, 200.0, 100.0, 50.0]) * rng.uniform(0.995, 1.005)
    b = _Builder(["CAN"], [1])
    frames = []
    t = rng.uniform(5, 30) * B
    while True:
        ident = rng.randrange(1 << 11)
        dlc = rng.randint(0, 8)
        data = [rng.randrange(256) for _ in range(dlc)]
        bits = [0] + _bits_msb(ident, 11) + [0, 0, 0] + _bits_msb(dlc, 4)
        for byte in data:
            bits += _bits_msb(byte)
        bits += _bits_msb(_can_crc(bits), 15)
        stuffed = _can_stuff(bits)
        tail = [1, 0, 1] + [1] * 7 + [1] * 3           # CRC delim, ACK, ACK delim, EOF, IFS
        total = stuffed + tail
        if t + len(total) * B > duration:
            break
        for i, v in enumerate(total):
            b.set(t + i * B, 0, v)
        frames.append(bits)
        t += len(total) * B + rng.expovariate(1 / (20 * B))
    return b.capture("CAN", duration, {"frames": frames}, dict(period=B))


# -- JTAG / SWD ---------------------------------------------------------------------------

def jtag(rng: random.Random, duration: int = 400_000, half: Optional[float] = None) -> Capture:
    H = half or _loguniform(rng, 3, 250)
    TCK, TMS, TDI, TDO = range(4)
    b = _Builder(["TCK", "TMS", "TDI", "TDO"], [0, 1, 1, 1])
    tdi_bits = []
    t = rng.uniform(1, 10) * H
    while True:
        n = rng.randint(8, 64)
        if t + (2 * n + 10) * H > duration:
            break
        tms = [1, 0, 0] + [0] * (n - 1) + [1, 1, 0]    # to shift state, shift n bits, exit
        for k, m in enumerate(tms):
            d = rng.randrange(2)
            b.set(t, TMS, m)
            b.set(t, TDI, d)
            b.set(t, TDO, rng.randrange(2))
            b.set(t + H, TCK, 1)                       # target samples TMS/TDI on the rise
            tdi_bits.append(d)
            b.set(t + 2 * H, TCK, 0)
            t += 2 * H
        t += rng.expovariate(1 / (40 * H)) + 2 * H
    return b.capture("JTAG/SWD", duration, {"tdi": tdi_bits}, dict(half=H, kind="JTAG"))


def swd(rng: random.Random, duration: int = 400_000, half: Optional[float] = None) -> Capture:
    H = half or _loguniform(rng, 3, 250)
    CLK, DIO = 0, 1
    b = _Builder(["SWCLK", "SWDIO"], [0, 1])
    host_bits = []
    t = rng.uniform(1, 10) * H

    def clock(bits):
        nonlocal t
        for v in bits:
            b.set(t, DIO, v)
            b.set(t + H, CLK, 1)
            b.set(t + 2 * H, CLK, 0)
            t += 2 * H

    reset = [1] * 52 + [0, 0]
    clock(reset)
    host_bits += reset
    while True:
        if t + 60 * 2 * H > duration:
            break
        apndp, rnw, addr = rng.randrange(2), rng.randrange(2), rng.randrange(4)
        par = (apndp + rnw + (addr & 1) + (addr >> 1)) & 1
        req = [1, apndp, rnw, addr & 1, addr >> 1, par, 0, 1]
        clock(req)
        host_bits += req
        clock([rng.randrange(2)])                     # turnaround
        clock([1, 0, 0])                              # ACK OK from the target
        clock([rng.randrange(2)])                     # turnaround
        word = [rng.randrange(2) for _ in range(32)]
        clock(word + [sum(word) & 1])
        host_bits += word
        t += rng.expovariate(1 / (20 * H)) + 2 * H
    return b.capture("JTAG/SWD", duration, {"host_bits": host_bits}, dict(half=H, kind="SWD"))


# -- PS/2 ---------------------------------------------------------------------------------

def ps2(rng: random.Random, duration: int = 1_200_000, half: Optional[float] = None) -> Capture:
    """Device-to-host PS/2 frames: DATA changes in the middle of CLK high, the host samples
    on CLK falling."""
    H = half or rng.uniform(1500, 2500)
    CLK, DATA = 0, 1
    b = _Builder(["CLK", "DATA"], [1, 1])
    data = []
    t = rng.uniform(1, 4) * H
    while True:
        if t + 24 * H > duration:
            break
        byte = rng.randrange(256)
        bits = [0] + _bits_lsb(byte) + [(sum(_bits_lsb(byte)) + 1) & 1, 1]
        for v in bits:
            b.set(t + H / 2, DATA, v)
            b.set(t + H, CLK, 0)                     # host samples here
            b.set(t + 2 * H, CLK, 1)
            t += 2 * H
        data.append(byte)
        t += rng.choice([2, 4, 40, 200]) * H * rng.uniform(0.8, 1.2)
    return b.capture("PS/2", duration, {"data": data}, dict(half=H))


# -- Manchester ---------------------------------------------------------------------------

def manchester(rng: random.Random, duration: int = 400_000, period: Optional[float] = None,
               pair: Optional[bool] = None) -> Capture:
    """IEEE 802.3 Manchester (0 = high-to-low, 1 = low-to-high at mid-bit), optionally as a
    differential pair, with a 1010... preamble before each burst."""
    B = period or _loguniform(rng, 8, 2000)
    pair = rng.random() < 0.3 if pair is None else pair
    lines = ["TX+", "TX-"] if pair else ["TX"]
    b = _Builder(lines, [1, 0][: len(lines)])
    bursts = []
    t = rng.uniform(2, 20) * B

    def drive(tt, v):
        b.set(tt, 0, v)
        if pair:
            b.set(tt, 1, 1 - v)

    while True:
        n = rng.randint(2, 20)
        bits = [1, 0] * 16 + [rng.randrange(2) for _ in range(8 * n)]
        if t + (len(bits) + 4) * B > duration:
            break
        for i, v in enumerate(bits):
            first, second = (1, 0) if v == 0 else (0, 1)
            drive(t + i * B, first)
            drive(t + i * B + B / 2, second)
        t += len(bits) * B
        drive(t, 1)                                  # idle high after the burst
        bursts.append(bits)
        t += rng.expovariate(1 / (30 * B)) + 3 * B
    return b.capture("Manchester", duration, {"bursts": bursts}, dict(period=B, pair=pair))


GENERATORS: Dict[str, Callable[..., Capture]] = {
    "UART": uart,
    "SPI": spi,
    "I2C": i2c,
    "USB-LS": usb_ls,
    "CAN": can,
    "JTAG/SWD": lambda rng, **kw: (jtag if rng.random() < 0.5 else swd)(rng, **kw),
    "PS/2": ps2,
    "Manchester": manchester,
}


def generate(label: str, rng: random.Random, **kw) -> Capture:
    return GENERATORS[label](rng, **kw)


# -- perturbation and pin assignment --------------------------------------------------------

def perturb(cap: Capture, rng: random.Random, jitter: int = 0, glitches: float = 0.0,
            glitch_width: Tuple[int, int] = (1, 2)) -> Capture:
    """Add uniform timing jitter (+/- jitter cycles, never reordering a line's edges) and
    short glitches (on average `glitches` per 10,000 cycles per line)."""
    per_line: Dict[int, List[Tuple[int, int]]] = {i: [] for i in range(len(cap.lines))}
    for t, ln, v in cap.edges:
        per_line[ln].append((t, v))
    out = []
    for ln, evs in per_line.items():
        prev = -1
        level = cap.initial[ln]
        moved = []
        for t, v in evs:
            tt = max(prev + 1, t + (rng.randint(-jitter, jitter) if jitter else 0))
            moved.append((tt, v))
            prev = tt
        # glitches: a pulse to the opposite level inside a stable stretch
        n = int(rng.expovariate(1.0) * glitches * cap.duration / 10_000) if glitches else 0
        for _ in range(n):
            g = rng.randrange(1, max(2, cap.duration - 4))
            w = rng.randint(*glitch_width)
            lvl, prev_t = cap.initial[ln], -1
            nxt = cap.duration
            for tt, v in moved:
                if tt <= g:
                    lvl, prev_t = v, tt
                else:
                    nxt = tt
                    break
            if prev_t < g and g + w + 1 < nxt:
                moved += [(g, 1 - lvl), (g + w, lvl)]
        moved.sort()
        for tt, v in moved:
            out.append((tt, ln, v))
    out = _clean(sorted(out), cap.initial)
    return Capture(cap.label, cap.lines, cap.initial, out, cap.duration, cap.payload,
                   {**cap.params, "jitter": jitter, "glitches": glitches})


def to_pins(cap: Capture, assignment: Sequence[Optional[int]], idle: Sequence[int] = (1, 1, 1, 1)
            ) -> Tuple[List[int], List[Tuple[int, int, int]]]:
    """Map a capture onto the Ear's 4 monitored pins. assignment[p] is the line index watched
    by pin p, or None for a pin left on an idle line at level idle[p]."""
    initial = [cap.initial[a] if a is not None else idle[p] for p, a in enumerate(assignment)]
    where = {a: p for p, a in enumerate(assignment) if a is not None}
    edges = [(t, where[ln], v) for t, ln, v in cap.edges if ln in where]
    return initial, edges


def random_assignment(cap: Capture, rng: random.Random, pins: int = 4) -> List[Optional[int]]:
    """Watch every line of the bus (up to `pins`), in a random pin order."""
    lines = list(range(len(cap.lines)))
    rng.shuffle(lines)
    lines = lines[:pins] + [None] * (pins - min(pins, len(lines)))
    rng.shuffle(lines)
    return lines
