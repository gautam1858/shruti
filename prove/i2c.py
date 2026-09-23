"""I2C master contracts (kind: i2c_master): one write transaction, START to STOP.

Both lines are open-drain, so each pad is the wired-AND of the program's own drives and a
symbolic slave. The slave pulls SDA low for the ACK of byte k (if ack_k) from D after the
falling edge that starts the ACK clock until D after the falling edge that ends it, and after
that edge holds SCL low for a symbolic S_k cycles (clock stretching). Its behaviour is defined
from the program's own SCL falling edges, counted from the START, so the pad signals are
rebuilt from the program's events every time the program reads a line.

The contract, checked on every feasible path with bytes, ACKs, stretch lengths, the slave's
delay and the half period all symbolic:
  - START: SDA falls while SCL is high;
  - every SCL low and high phase at the pads lasts at least P, stretching included;
  - SDA is stable while SCL is high, carries each byte MSB first, and carries the slave's
    ACK or NACK in the ninth clock;
  - bytes are clocked until the first NACK or until the bytes run out, then a STOP: SDA rises
    while SCL is high, at least P after SCL rose;
  - SYNC flag 1 is set exactly when the transaction ended in a NACK;
  - never LATE, never HALT, and the program's own drives on each line come in time order.

    program: i2c_master.s
    kind: i2c_master
    assume:
      P: {min: 65, max: 2000}
      pins: {scl: B6, sda: B7}
      cfg: {OUT_MSB: 1, OUT_N: 7}
      bytes: [d0, d1]                    # queued together
      stretch: {max: 1000}               # cycles the slave may hold SCL after each ACK
      slave_delay: {min: 1, max: 2}
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
import z3

from asm import Assembler

from .contract import ContractError, _pin
from .symex import AndSignal, Byte, SymExec, Wave

RECENT = 6                                        # drive events a WAIT considers


@dataclass
class I2cContract:
    path: Path
    program: Path
    P_min: int
    P_max: int
    scl: int
    sda: int
    cfg: Dict[str, int]
    bytes: List[str]
    stretch_max: int
    d_min: int
    d_max: int
    loop: str


def load_i2c(path) -> I2cContract:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if raw.get("kind") != "i2c_master":
        raise ContractError(f"{path}: not an i2c_master contract")
    a = raw["assume"]
    return I2cContract(path, path.parent / raw["program"], int(a["P"]["min"]), int(a["P"]["max"]),
                       _pin(a["pins"]["scl"]), _pin(a["pins"]["sda"]), dict(a.get("cfg") or {}),
                       list(a["bytes"]), int(a["stretch"]["max"]), int(a["slave_delay"]["min"]),
                       int(a["slave_delay"]["max"]), raw.get("loop", "top"))


class _Master:
    """The program's open-drain drive of one line: released (1) until its first event."""

    def __init__(self, events, pin):
        self.ev = [e for e in events if e.pin == pin]
        self.wave = Wave(z3.BoolVal(True), [e.time for e in self.ev], [e.level for e in self.ev])

    def level_at(self, t):
        return self.wave.level_at(t)

    def boundaries(self):
        return self.wave.boundaries()


class _Pad:
    """Wired-AND of the master's drive and the slave, with recent/older edge split."""

    def __init__(self, master: _Master, slave: Wave):
        self.m, self.s = master, slave
        self.sig = AndSignal(master, slave)

    def level_at(self, t):
        return self.sig.level_at(t)

    def boundaries(self):
        return self.sig.boundaries()

    def split_boundaries(self):
        mb = self.m.boundaries()
        return mb[:-RECENT], mb[-RECENT:] + self.s.boundaries()


class Slave:
    def __init__(self, c: I2cContract):
        n = len(c.bytes)
        self.c = c
        self.ack = [z3.Bool(f"ack_{k}") for k in range(n)]
        self.S = [z3.Int(f"stretch_{k}") for k in range(n)]
        self.D = z3.Int("slave_delay")

    def falls_after_start(self, events):
        """Emission indices: the START (first SDA drive low) and the SCL falls after it."""
        start = next((i for i, e in enumerate(events)
                      if e.pin == self.c.sda and z3.is_false(z3.simplify(e.level))), None)
        if start is None:
            return None, []
        falls = [e for e in events[start + 1:]
                 if e.pin == self.c.scl and z3.is_false(z3.simplify(e.level))]
        return events[start], falls

    def waves(self, events):
        _, F = self.falls_after_start(events)
        n = len(self.c.bytes)
        st, sl, dt, dl = [], [], [], []               # SCL hold, SDA pull
        for k in range(n):
            if len(F) > 9 * k + 8:
                dt.append(F[9 * k + 8].time + self.D)
                dl.append(z3.Not(self.ack[k]))
                if len(F) > 9 * k + 9:
                    end = F[9 * k + 9].time + self.D
                    dt.append(end)
                    dl.append(z3.BoolVal(True))
                    st += [end, end + self.S[k]]
                    sl += [z3.BoolVal(False), z3.BoolVal(True)]
        return Wave(z3.BoolVal(True), st, sl), Wave(z3.BoolVal(True), dt, dl)

    def pads(self, events):
        scl_s, sda_s = self.waves(events)
        return (_Pad(_Master(events, self.c.scl), scl_s),
                _Pad(_Master(events, self.c.sda), sda_s))


@dataclass
class I2cOutcome:
    proved: bool
    seconds: float
    P_range: Tuple[int, int]
    paths: int
    failed: List[str] = field(default_factory=list)
    model: Optional[dict] = None

    def report(self) -> str:
        lo, hi = self.P_range
        if self.proved:
            return (f"PROVED for P in {lo}..{hi}, all bytes, every ACK/NACK pattern, clock "
                    f"stretching and slave delays within the contract ({self.paths} paths, "
                    f"{self.seconds:.1f} s)")
        lines = [f"COUNTEREXAMPLE ({self.seconds:.1f} s): {self.model}"]
        lines += [f"  violated: {f}" for f in self.failed]
        return "\n".join(lines)


def _stable(pad: _Pad, a, b, level) -> z3.BoolRef:
    """The pad holds `level` at a and has no change in (a, b]."""
    conds = [pad.level_at(a) == level]
    for x in pad.boundaries():
        conds.append(z3.Not(z3.And(x > a, x <= b, pad.level_at(x) != pad.level_at(x - 1))))
    return z3.And(*conds)


def prove_i2c(c: I2cContract, source: Optional[str] = None, P_range=None) -> I2cOutcome:
    t0 = time.perf_counter()
    asm = Assembler()
    words = asm.assemble(source if source is not None else c.program.read_text(), str(c.program))
    loop = asm.labels[c.loop]
    lo, hi = P_range or (c.P_min, c.P_max)
    P = z3.Int("P")
    slave = Slave(c)
    a0 = z3.Int("arrival")
    tx = [Byte(z3.BitVec(n, 8), a0) for n in c.bytes]           # queued together
    assume = [P >= lo, P <= hi, a0 >= 0, slave.D >= c.d_min, slave.D <= c.d_max]
    assume += [z3.And(S >= 0, S <= c.stretch_max) for S in slave.S]
    inputs = {c.scl: lambda s: slave.pads(s.events)[0], c.sda: lambda s: slave.pads(s.events)[1]}
    found: dict = {}
    n = len(tx)

    def check(r) -> bool:
        checks: List[Tuple[str, z3.BoolRef]] = []
        fail = lambda why: checks.append((why, z3.BoolVal(False)))
        for ob in r.obligations:
            checks.append((f"{ob.name} (word {ob.pc})", ob.cond))
        if r.reason != "stop_at":
            fail(f"the transaction does not return to '{c.loop}' ({r.reason})")
        ev = r.events
        for pin in (c.scl, c.sda):
            pe = [e for e in ev if e.pin == pin]
            checks.append((f"drives on pin {pin} come in time order",
                           z3.And(*[x.time <= y.time for x, y in zip(pe, pe[1:])])))
        scl, sda = slave.pads(ev)
        start, _ = slave.falls_after_start(ev)
        if start is None:
            fail("no START")
        else:
            i0 = ev.index(start)
            # the transaction's events: from the START up to the SETs of the next pass
            end = next((j for j in range(i0 + 1, len(ev)) if ev[j].key[0] == 1), len(ev))
            txev = ev[i0 + 1:end]
            clk = [e for e in txev if e.pin == c.scl]
            falls = [e for e in clk if z3.is_false(z3.simplify(e.level))]
            rels = [e for e in clk if z3.is_true(z3.simplify(e.level))]
            sda_after = [e for e in txev if e.pin == c.sda]
            m = len(falls) - 1
            if len(rels) != len(falls) or m % 9 != 0 or not 1 <= m // 9 <= n:
                fail(f"structure: {len(falls)} SCL falls and {len(rels)} releases after START")
            else:
                nb = m // 9
                t_s = start.time
                checks.append(("START: SDA falls while SCL is high",
                               z3.And(scl.level_at(t_s), sda.level_at(t_s - 1),
                                      z3.Not(sda.level_at(t_s)))))
                F = [e.time for e in falls]
                pr = []
                for i, R in enumerate(rels):
                    rise = R.time
                    if i >= 9 and i % 9 == 0 and (i // 9 - 1) < n:
                        k = i // 9 - 1
                        hold_end = F[i] + slave.D + slave.S[k]
                        rise = z3.If(hold_end > rise, hold_end, rise)
                    pr.append(rise)
                    checks.append((f"clock {i}: SCL low at least P", pr[i] - F[i] >= P))
                    checks.append((f"clock {i}: SCL pad rises when released",
                                   z3.And(scl.level_at(pr[i]), z3.Not(scl.level_at(pr[i] - 1)))))
                    if i < m:
                        checks.append((f"clock {i}: SCL high at least P", F[i + 1] - pr[i] >= P))
                for k in range(nb):
                    d = tx[k].data
                    for j in range(9):
                        i = 9 * k + j
                        want = (z3.Extract(7 - j, 7 - j, d) == 1) if j < 8 else z3.Not(slave.ack[k])
                        what = f"byte {k} bit {j}" if j < 8 else f"byte {k} ACK"
                        checks.append((f"{what}: SDA correct and stable while SCL is high",
                                       z3.And(sda.level_at(pr[i] - 1) == want,
                                              _stable(sda, pr[i], F[i + 1], want))))
                acked = [slave.ack[k] for k in range(nb)]
                ended_by_nack = z3.Not(z3.And(*acked))
                checks.append(("bytes are clocked until the first NACK or the last byte",
                               z3.And(z3.And(*acked[:-1]),
                                      z3.BoolVal(True) if nb == n else z3.Not(acked[-1]))))
                flagged = any(f == 1 for f, _ in r.sync_set)
                checks.append(("SYNC flag 1 set exactly when the transaction ended in a NACK",
                               ended_by_nack if flagged else z3.Not(ended_by_nack)))
                t_stop = sda_after[-1].time if sda_after else None
                if t_stop is None or not z3.is_true(z3.simplify(sda_after[-1].level)):
                    fail("no STOP (SDA is not released at the end)")
                else:
                    checks.append(("STOP: SDA rises while SCL is high, at least P after SCL rose",
                                   z3.And(t_stop - pr[m] >= P, scl.level_at(t_stop),
                                          z3.Not(sda.level_at(t_stop - 1)), sda.level_at(t_stop),
                                          _stable(sda, pr[m], t_stop - 1, z3.BoolVal(False)))))
        s = z3.Solver()
        s.add(*assume, *r.path)
        s.add(z3.Not(z3.And(*[cond for _, cond in checks])))
        if s.check() != z3.sat:
            return False
        mdl = s.model()
        val = lambda x: mdl.eval(x, model_completion=True)
        found["model"] = {"P": val(P), "bytes": [val(b.data) for b in tx],
                          "ack": [val(a) for a in slave.ack], "stretch": [val(x) for x in slave.S],
                          "delay": val(slave.D)}
        found["failed"] = list(dict.fromkeys(nm for nm, cond in checks
                                             if z3.is_false(mdl.eval(cond, model_completion=True))))
        return True

    paths = SymExec(words, P, tx, cfg=c.cfg, inputs=inputs, stop_at=(loop, 2),
                    max_steps=20000).paths(assume, on_path=check)
    secs = time.perf_counter() - t0
    if found:
        return I2cOutcome(False, secs, (lo, hi), len(paths), found["failed"], found["model"])
    return I2cOutcome(True, secs, (lo, hi), len(paths))
