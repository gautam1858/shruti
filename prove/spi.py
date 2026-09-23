"""SPI master contracts (kind: spi_master), mode 0, MSB first, one byte per chip select.

The program runs with symbolic bytes, symbolic arrival times and a symbolic half period P.
MISO samples are deferred: each IN returns a fresh variable, and after execution each one is
tied to the slave's MISO waveform, which the contract builds from the program's own CS and SCK
events: the slave drives bit 7 a delay D after CS falls and each later bit D after an SCK
falling edge, and releases MISO (high) D after CS rises. The program's events are referred to
by their order of emission, which the structure check confirms first.

    program: spi_master.s
    kind: spi_master
    assume:
      P: {min: 5, max: 65535}            # half the SCK period
      pins: {sck: B2, mosi: B3, miso: M1, cs: B5}
      tx_fifo: [d0, d1]                  # bytes sent, symbolic arrival times
      miso: [m0, m1]                     # bytes the slave returns
      slave_delay: {min: 1, max: 2}      # cycles from an edge at the slave's pin to MISO
    guarantee:
      never: [LATE, UNF, HALT]
      # per byte: CS low for the transfer and high for at least P between transfers; SCK
      # idles low; 8 SCK pulses, high for exactly P and low for exactly P; MOSI carries the
      # byte MSB first and is stable across every rising edge; the RX FIFO receives the
      # slave's byte.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
import z3

from asm import assemble
from iss import Sim

from .check import PinTimeline
from .contract import ContractError, _pin
from .symex import Byte, SymExec, Wave


@dataclass
class SpiContract:
    path: Path
    program: Path
    P_min: int
    P_max: int
    pins: Dict[str, int]
    tx: List[str]
    miso: List[str]
    d_min: int
    d_max: int
    never: List[str]


def load_spi(path) -> SpiContract:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if raw.get("kind") != "spi_master":
        raise ContractError(f"{path}: not an spi_master contract")
    a, g = raw["assume"], raw["guarantee"]
    return SpiContract(path, path.parent / raw["program"], int(a["P"]["min"]), int(a["P"]["max"]),
                       {k: _pin(v) for k, v in a["pins"].items()}, list(a["tx_fifo"]),
                       list(a["miso"]), int(a["slave_delay"]["min"]), int(a["slave_delay"]["max"]),
                       list(g.get("never", [])))


@dataclass
class SpiOutcome:
    proved: bool
    seconds: float
    P_range: Tuple[int, int]
    failed: List[str] = field(default_factory=list)
    model: Optional[dict] = None
    iss_ok: Optional[bool] = None

    def report(self) -> str:
        lo, hi = self.P_range
        if self.proved:
            return (f"PROVED for P in {lo}..{hi}, all bytes in both directions, arrival times "
                    f"and slave delays within the contract ({self.seconds:.2f} s)")
        lines = [f"COUNTEREXAMPLE ({self.seconds:.2f} s): {self.model}"]
        lines += [f"  violated: {f}" for f in self.failed]
        if self.iss_ok is not None:
            lines.append(f"  ISS confirms the violation: {not self.iss_ok}")
        return "\n".join(lines)


def prove_spi(c: SpiContract, words: Optional[List[int]] = None, P_range=None) -> SpiOutcome:
    t0 = time.perf_counter()
    words = words if words is not None else assemble(c.program.read_text(), str(c.program))
    lo, hi = P_range or (c.P_min, c.P_max)
    P, D = z3.Int("P"), z3.Int("D")
    tx = [Byte(z3.BitVec(n, 8), z3.Int(f"a_{n}")) for n in c.tx]
    mi = [z3.BitVec(n, 8) for n in c.miso]
    pins = c.pins
    assume = [P >= lo, P <= hi, D >= c.d_min, D <= c.d_max]
    prev = z3.IntVal(0)
    for b in tx:
        assume.append(b.arrival >= prev)
        prev = b.arrival
    res = SymExec(words, P, tx, deferred=[pins["miso"]]).run()
    n = len(tx)
    ev = {k: [e for e in res.events if e.pin == p] for k, p in pins.items() if k != "miso"}
    fail = lambda why: SpiOutcome(False, time.perf_counter() - t0, (lo, hi), [why])
    if not (len(ev["cs"]) == 1 + 2 * n and len(ev["sck"]) == 1 + 16 * n
            and len(ev["mosi"]) == 8 * n and len(res.deferred) == 8 * n and len(res.pushed) == n):
        return fail("structure: expected one CS pulse, 8 SCK pulses, 8 MOSI bits and 8 MISO "
                    f"samples per byte (CS {len(ev['cs'])}, SCK {len(ev['sck'])}, "
                    f"MOSI {len(ev['mosi'])}, samples {len(res.deferred)}, pushed {len(res.pushed)})")
    defs: list = []
    tl = {k: PinTimeline(res.events, pins[k], True, defs, assume) for k in ("cs", "sck", "mosi")}
    t = lambda k, i: ev[k][i].time
    checks: List[Tuple[str, z3.BoolRef]] = []
    for ob in res.obligations:
        checks.append((f"{ob.name} (word {ob.pc})", ob.cond))
    # the slave's MISO waveform, from the program's own CS and SCK events
    mtimes, mlevels = [], []
    for k in range(n):
        cs_fall, cs_rise = t("cs", 1 + 2 * k), t("cs", 2 + 2 * k)
        mtimes.append(cs_fall + D)
        mlevels.append(z3.Extract(7, 7, mi[k]) == 1)
        for j in range(7):
            mtimes.append(t("sck", 2 + 16 * k + 2 * j) + D)
            mlevels.append(z3.Extract(6 - j, 6 - j, mi[k]) == 1)
        mtimes.append(cs_rise + D)
        mlevels.append(z3.BoolVal(True))
    miso = Wave(z3.BoolVal(True), mtimes, mlevels)
    ties = [v == miso.level_at(when - 2) for v, _pin_, when in res.deferred]
    for k in range(n):
        cs_fall, cs_rise = t("cs", 1 + 2 * k), t("cs", 2 + 2 * k)
        r = [t("sck", 1 + 16 * k + 2 * j) for j in range(8)]
        f = [t("sck", 2 + 16 * k + 2 * j) for j in range(8)]
        checks.append((f"byte {k}: CS low for the whole transfer",
                       tl["cs"].holds_on(cs_fall, cs_rise, z3.BoolVal(False))))
        if k + 1 < n:
            nxt = t("cs", 3 + 2 * k)
            checks.append((f"byte {k}: CS high for at least P before the next byte",
                           z3.And(nxt - cs_rise >= P, tl["cs"].holds_on(cs_rise, nxt, z3.BoolVal(True)))))
        checks.append((f"byte {k}: SCK low from CS falling to the first rising edge, at least P",
                       z3.And(r[0] - cs_fall >= P, tl["sck"].holds_on(cs_fall, r[0], z3.BoolVal(False)))))
        for j in range(8):
            checks.append((f"byte {k} bit {j}: SCK high for exactly P",
                           z3.And(f[j] - r[j] == P, tl["sck"].holds_on(r[j], f[j], z3.BoolVal(True)))))
            end = r[j + 1] if j < 7 else cs_rise
            low = (end - f[j] == P) if j < 7 else (end - f[j] >= P)
            checks.append((f"byte {k} bit {j}: SCK low for {'exactly' if j < 7 else 'at least'} P",
                           z3.And(low, tl["sck"].holds_on(f[j], end, z3.BoolVal(False)))))
            want = z3.Extract(7 - j, 7 - j, tx[k].data) == 1
            checks.append((f"byte {k} bit {j}: MOSI correct and stable across the rising edge",
                           z3.And(tl["mosi"].level_at(r[j] - 1) == want,
                                  tl["mosi"].level_at(r[j]) == want,
                                  tl["mosi"].level_at(r[j] + 1) == want)))
        checks.append((f"byte {k}: RX FIFO receives the slave's byte",
                       z3.Extract(7, 0, res.pushed[k][1]) == mi[k]))
    s = z3.Solver()
    s.add(*assume, *defs, *ties, *res.path)
    if s.check() != z3.sat:
        return fail("vacuous: the assumptions contradict each other")
    s.add(z3.Not(z3.And(*[cond for _, cond in checks])))
    r = s.check()
    secs = time.perf_counter() - t0
    if r == z3.unsat:
        return SpiOutcome(True, secs, (lo, hi))
    m = s.model()
    val = lambda x: m.eval(x, model_completion=True).as_long()
    model = {"P": val(P), "D": val(D), "tx": [val(b.data) for b in tx],
             "arrival": [val(b.arrival) for b in tx], "miso": [val(x) for x in mi]}
    failed = list(dict.fromkeys(nm for nm, cond in checks
                                if z3.is_false(m.eval(cond, model_completion=True))))
    out = SpiOutcome(False, secs, (lo, hi), failed, model)
    out.iss_ok = _replay(c, words, model)
    return out


def _replay(c: SpiContract, words, m) -> bool:
    """Run the counterexample on the ISS against the Python mode-0 slave (which answers one
    cycle after it sees an edge: D = 1 only). True if the ISS run meets the contract."""
    if m["D"] != 1:
        return None
    from fw.peers import SpiSlave
    slave = SpiSlave(c.pins["sck"], c.pins["mosi"], c.pins["miso"], c.pins["cs"],
                     responses=list(m["miso"]))
    sim = Sim([words], P=[m["P"]], devices=[slave],
              host_tx=[[(a, d) for a, d in zip(m["arrival"], m["tx"])]])
    res = sim.run(max(m["arrival"]) + 40 * m["P"] * (len(m["tx"]) + 1) + 1000)
    return (slave.received == m["tx"] and res.host_rx[0] == m["miso"] and not slave.errors
            and not res.ems[0].LATE)
