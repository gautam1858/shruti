"""Contracts for programs that react to input pins (kind: receiver).

The peer is described symbolically: 8N1 frames on an input pin with symbolic data bytes,
symbolic start cycles, the program's bit period P, and a bounded per-edge jitter. The
executor forks on every branch that depends on the input (WAIT, JMP on the pin) and keeps the
feasible paths; each path must deliver exactly the transmitted bytes to the RX FIFO, never set
the framing-error flag, and never be late. A counterexample is replayed on the concrete ISS.

    program: uart_rx.s
    kind: receiver
    assume:
      P: {min: 16, max: 65535}
      rx:
        pin: M0
        frames: [d0, d1]            # symbolic bytes, one 8N1 frame each
        first_start: 20             # the line idles high at least until this cycle
        jitter: "P//16"             # each edge may move by up to this many cycles
    guarantee:
      never: [LATE, HALT]
      rx_fifo: frames               # exactly the transmitted bytes, in order
      sync_never: [0]               # SYNC flags that must stay clear
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
import z3

from asm import assemble
from iss import simulate

from .contract import ContractError, _pin, eval_expr
from .symex import SymExec, Wave


@dataclass
class ReceiverContract:
    path: Path
    program: Path
    P_min: int
    P_max: int
    pin: int
    frames: List[str]
    first_start: int
    jitter: str
    never: List[str]
    sync_never: List[int]


def load_receiver(path) -> ReceiverContract:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if raw.get("kind") != "receiver":
        raise ContractError(f"{path}: not a receiver contract")
    a, g = raw["assume"], raw["guarantee"]
    rx = a["rx"]
    return ReceiverContract(path, path.parent / raw["program"], int(a["P"]["min"]),
                            int(a["P"]["max"]), _pin(rx["pin"]), list(rx["frames"]),
                            int(rx.get("first_start", 0)), str(rx.get("jitter", 0)),
                            list(g.get("never", [])), list(g.get("sync_never", [])))


@dataclass
class ReceiverOutcome:
    proved: bool
    seconds: float
    P_range: Tuple[int, int]
    paths: int
    model: Optional[dict] = None
    failed: List[str] = field(default_factory=list)
    iss_rx: Optional[List[int]] = None
    iss_confirms: Optional[bool] = None

    def report(self) -> str:
        lo, hi = self.P_range
        if self.proved:
            return (f"PROVED for P in {lo}..{hi}, all data bytes, start times and edge jitter "
                    f"within the contract ({self.paths} paths, {self.seconds:.2f} s)")
        m = self.model
        lines = [f"COUNTEREXAMPLE ({self.seconds:.2f} s): P = {m['P']}, bytes "
                 + ", ".join(f"0x{d:02X}" for d in m["data"])
                 + ", starts " + ", ".join(str(s) for s in m["starts"])]
        lines += [f"  violated: {f}" for f in self.failed]
        lines.append(f"  ISS received: {[hex(b) for b in (self.iss_rx or [])]}")
        lines.append(f"  ISS confirms the violation: {self.iss_confirms}")
        return "\n".join(lines)


def _frames_wave(c: ReceiverContract, P, jitter_bound):
    """The peer's pad waveform, its variables and the assumptions that shape it."""
    data = [z3.BitVec(n, 8) for n in c.frames]
    starts = [z3.Int(f"start_{i}") for i in range(len(data))]
    jit = [[z3.Int(f"jit_{i}_{k}") for k in range(10)] for i in range(len(data))]
    times, levels, assume = [], [], []
    for i, d in enumerate(data):
        bits = [z3.BoolVal(False)] + [z3.Extract(k, k, d) == 1 for k in range(8)] + [z3.BoolVal(True)]
        for k, v in enumerate(bits):
            times.append(starts[i] + k * P + jit[i][k])
            levels.append(v)
            assume += [jit[i][k] >= -jitter_bound, jit[i][k] <= jitter_bound]
        assume.append(jit[i][0] == 0)                       # the start edge defines the frame
        if i == 0:
            assume.append(starts[0] >= c.first_start)
        else:
            assume.append(starts[i] >= starts[i - 1] + 10 * P)   # a full stop bit between frames
    return Wave(z3.BoolVal(True), times, levels), data, starts, jit, assume


def prove_receiver(c: ReceiverContract, words: Optional[List[int]] = None,
                   P_range=None) -> ReceiverOutcome:
    t0 = time.perf_counter()
    words = words if words is not None else assemble(c.program.read_text(), str(c.program))
    lo, hi = P_range or (c.P_min, c.P_max)
    P = z3.Int("P")
    J = eval_expr(c.jitter, {"P": P}, None)
    wave, data, starts, jit, assume = _frames_wave(c, P, J)
    assume = [P >= lo, P <= hi] + assume
    found: dict = {}

    def check_path(r) -> bool:
        checks: List[Tuple[str, z3.BoolRef]] = []
        for ob in r.obligations:
            checks.append((f"{ob.name} (word {ob.pc})", ob.cond))
        if len(r.pushed) != len(data):
            checks.append((f"RX FIFO receives {len(data)} bytes (this path pushes "
                           f"{len(r.pushed)})", z3.BoolVal(False)))
        else:
            for k, ((_, v), d) in enumerate(zip(r.pushed, data)):
                checks.append((f"byte {k} received correctly", z3.Extract(7, 0, v) == d))
        for flag, _t in r.sync_set:
            if flag in c.sync_never:
                checks.append((f"SYNC flag {flag} never set", z3.BoolVal(False)))
        s = z3.Solver()
        s.add(*assume, *r.path)
        s.add(z3.Not(z3.And(*[cond for _, cond in checks])) if checks else z3.BoolVal(False))
        if s.check() != z3.sat:
            return False
        m = s.model()
        val = lambda x: m.eval(x, model_completion=True).as_long()
        found["model"] = {"P": val(P), "data": [val(d) for d in data],
                          "starts": [val(st) for st in starts],
                          "jitter": [[val(j) for j in row] for row in jit]}
        found["failed"] = list(dict.fromkeys(n for n, cond in checks
                                             if z3.is_false(m.eval(cond, model_completion=True))))
        return True

    res_paths = SymExec(words, P, [], inputs={c.pin: wave}).paths(assume, on_path=check_path)
    if found:
        out = ReceiverOutcome(False, time.perf_counter() - t0, (lo, hi), len(res_paths),
                              found["model"], found["failed"])
        _replay(c, words, out)
        return out
    return ReceiverOutcome(True, time.perf_counter() - t0, (lo, hi), len(res_paths))


def _replay(c: ReceiverContract, words, out: ReceiverOutcome) -> None:
    m = out.model
    P = m["P"]
    edges, level = [], 1
    for d, s, js in zip(m["data"], m["starts"], m["jitter"]):
        bits = [0] + [(d >> k) & 1 for k in range(8)] + [1]
        for k, v in enumerate(bits):
            if v != level:
                edges.append((s + k * P + js[k], c.pin, v))
                level = v
    horizon = max(m["starts"]) + 20 * P + 100
    res = simulate(words, horizon, P=P, stimulus=edges)
    out.iss_rx = res.host_rx[0]
    em = res.ems[0]
    bad_flags = any(res.sync[f] for f in c.sync_never)
    out.iss_confirms = bool(res.host_rx[0] != m["data"] or bad_flags or em.LATE or em.halted)
