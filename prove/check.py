"""The prover: symbolic execution plus Z3 against a contract, with ISS-confirmed
counterexamples.

The waveform check works on drive events. A pin's level at time t is the level of the last
event at or before t (ties broken by event order), or the initial external level if there
is none. "Level v on [s, e)" is encoded as: the level at s is v, and no event strictly inside
(s, e) drives the other level. Frame k starts at the first drive to the active level at or
after the previous frame's end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import z3

from asm import assemble
from iss import simulate

from .contract import Contract, eval_expr, expand_waveform, load_contract
from .symex import Byte, Event, Obligation, SymExec, SymResult

class PinTimeline:
    """The drive events of one pin, with each event's time and the pairwise "j is applied after
    i" relation named once as Z3 constants, so level queries stay small."""

    def __init__(self, events: Sequence[Event], pin: int, initial: bool, defs: list,
                 assume: Sequence[z3.BoolRef] = ()):
        self.evs = [e for e in events if e.pin == pin]
        self.initial = z3.BoolVal(initial)
        n = len(self.evs)
        uid = id(self)
        self.t = [z3.Int(f"t{pin}_{i}_{uid}") for i in range(n)]
        self.v = [z3.Bool(f"v{pin}_{i}_{uid}") for i in range(n)]
        for i, e in enumerate(self.evs):
            defs.append(self.t[i] == e.time)
            defs.append(self.v[i] == e.level)
        self.after = {}
        for i in range(n):
            for j in range(n):
                if i != j:
                    ki, kj = self.evs[i].key, self.evs[j].key
                    self.after[j, i] = (self.t[j] > self.t[i]) if kj < ki else (self.t[j] >= self.t[i])
        # If the events are provably emitted in application order (time, then key), the level
        # at t is a linear If-chain; otherwise use the general pairwise encoding.
        self.ordered = False
        if n > 1:
            order = []
            for i in range(n - 1):
                a, b = self.evs[i], self.evs[i + 1]
                order.append(self.t[i] < self.t[i + 1] if b.key < a.key
                             else self.t[i] <= self.t[i + 1])
            chk = z3.Solver()
            chk.add(*assume, *defs, z3.Not(z3.And(*order)))
            self.ordered = chk.check() == z3.unsat

    def level_at(self, t):
        n = len(self.evs)
        if self.ordered or n <= 1:
            lvl = self.initial
            for i in range(n):
                lvl = z3.If(self.t[i] <= t, self.v[i], lvl)
            return lvl
        lvl = self.initial
        for i in reversed(range(n)):
            cond = z3.And(self.t[i] <= t,
                          *[z3.Not(z3.And(self.t[j] <= t, self.after[j, i]))
                            for j in range(n) if j != i])
            lvl = z3.If(cond, self.v[i], lvl)
        return lvl

    def holds_on(self, s, e, level):
        """Level `level` over [s, e) (vacuous if e <= s); e = None means forever."""
        if e is None:
            inside = [z3.Not(z3.And(t > s, v != level)) for t, v in zip(self.t, self.v)]
            return z3.And(self.level_at(s) == level, *inside)
        inside = [z3.Not(z3.And(t > s, t < e, v != level)) for t, v in zip(self.t, self.v)]
        return z3.Implies(s < e, z3.And(self.level_at(s) == level, *inside))

    def first_drive(self, level, after):
        """(found, time) of the earliest drive to `level` at or after `after`."""
        found, best = z3.BoolVal(False), z3.IntVal(0)
        for t, v in zip(self.t, self.v):
            ok = z3.And(v == level, t >= after)
            best = z3.If(z3.And(ok, z3.Or(z3.Not(found), t < best)), t, best)
            found = z3.Or(found, ok)
        return found, best


@dataclass
class Frame:
    start: z3.ArithRef
    end: z3.ArithRef


def contract_formula(c: Contract, events, P, bytes_: Sequence[Byte], assume=()):
    """(property, frames, definitions) for the given drive events."""
    idle = z3.BoolVal(bool(c.idle_level))
    defs: list = []
    tl = PinTimeline(events, c.pin, bool(c.initial.get(c.pin, 1)), defs, assume)
    segs = expand_waveform(c.waveform)
    bit = lambda d, i: z3.Extract(i, i, d) == 1
    props = []
    frames = []
    prev_end = z3.IntVal(c.idle_from)
    for b in bytes_:
        found, s = tl.first_drive(z3.Not(idle), prev_end)
        props.append(found)
        env = {"P": P, "d": b.data}
        end = s + eval_expr(c.length, env, bit)
        props.append(s <= _max(prev_end, b.arrival) + c.start_within)
        props.append(tl.holds_on(prev_end, s, idle))
        for seg in segs:
            env_k = {**env, "k": seg.get("_k", 0)}
            f = eval_expr(seg["from"], env_k, bit)
            t = eval_expr(seg["to"], env_k, bit)
            lv = eval_expr(seg["level"], env_k, bit)
            lv = lv if isinstance(lv, z3.BoolRef) else z3.BoolVal(bool(lv))
            props.append(tl.holds_on(s + f, s + t, lv))
        frames.append(Frame(s, end))
        prev_end = end
    props.append(tl.holds_on(prev_end, None, idle))
    return z3.And(*props), frames, defs


def _max(a, b):
    return z3.If(a >= b, a, b)


@dataclass
class Outcome:
    proved: bool
    seconds: float
    P_range: Tuple[int, int]
    model: Optional[dict] = None             # P, bytes, arrivals
    failed: List[str] = field(default_factory=list)
    iss_trace: Optional[List[Tuple[int, int]]] = None
    iss_confirms: Optional[bool] = None
    expected: Optional[List[Tuple[int, int, int]]] = None   # (from, to, level) per segment
    first_mismatch: Optional[Tuple[int, int, int]] = None   # (cycle, expected, iss level)

    def report(self) -> str:
        lo, hi = self.P_range
        if self.proved:
            return f"PROVED for P in {lo}..{hi}, all data bytes and arrival times ({self.seconds:.2f} s)"
        m = self.model
        lines = [f"COUNTEREXAMPLE ({self.seconds:.2f} s): P = {m['P']}, "
                 + ", ".join(f"byte {i} = 0x{d:02X} at cycle {a}"
                             for i, (d, a) in enumerate(zip(m['data'], m['arrival'])))]
        lines += [f"  violated: {f}" for f in self.failed]
        if self.first_mismatch:
            c, want, got = self.first_mismatch
            lines.append(f"  first mismatch on the ISS: cycle {c}, expected {want}, pin is {got}")
        lines.append("  ISS pin trace (cycle, level): "
                     + ", ".join(f"({c}, {v})" for c, v in (self.iss_trace or [])[:40]))
        lines.append(f"  ISS confirms the violation: {self.iss_confirms}")
        return "\n".join(lines)


def prove(c: Contract, words: Optional[List[int]] = None, P_range=None) -> Outcome:
    t0 = time.perf_counter()
    if words is None:
        words = assemble(c.program.read_text(), str(c.program))
    lo, hi = P_range or (c.P_min, c.P_max)
    P = z3.Int("P")
    bytes_ = [Byte(z3.BitVec(f["data"], 8), z3.Int(f["at"])) for f in c.fifo[: c.frames_bound]]
    res = SymExec(words, P, bytes_, cfg=c.cfg).run()

    assume = [P >= lo, P <= hi]
    prev = z3.IntVal(0)
    for b in bytes_:
        assume.append(b.arrival >= prev)
        prev = b.arrival

    checks: List[Tuple[str, z3.BoolRef]] = []
    for ob in res.obligations:
        flag = ob.name.split()[1] if ob.name.startswith("never") else None
        if flag is None or flag in c.never or ob.name.startswith("never LATE"):
            checks.append((f"{ob.name} (word {ob.pc})", ob.cond))
    prop, frames, defs = contract_formula(c, res.events, P, bytes_, assume)
    checks.append(("waveform", prop))

    s = z3.Solver()
    s.add(*assume, *defs)
    s.add(z3.Not(z3.And(*[cond for _, cond in checks])))
    r = s.check()
    secs = time.perf_counter() - t0
    if r == z3.unsat:
        return Outcome(True, secs, (lo, hi))
    if r != z3.sat:
        raise RuntimeError(f"solver returned {r}")
    m = s.model()
    val = lambda x: m.eval(x, model_completion=True).as_long()
    model = {"P": val(P), "data": [val(b.data) for b in bytes_],
             "arrival": [val(b.arrival) for b in bytes_]}
    failed = list(dict.fromkeys(name for name, cond in checks
                                if z3.is_false(m.eval(cond, model_completion=True))))
    out = Outcome(False, secs, (lo, hi), model, failed)
    _confirm_on_iss(c, words, out)
    return out


def _confirm_on_iss(c: Contract, words, out: Outcome) -> None:
    """Re-run the counterexample on the concrete ISS and check the contract on its trace."""
    m = out.model
    horizon = max(m["arrival"] + [0]) + 30 * m["P"] * (len(m["data"]) + 2) + 1000
    res = simulate(words, horizon, P=m["P"], initial=c.initial, cfg=c.cfg or None,
                   host_tx=[(a, d) for a, d in zip(m["arrival"], m["data"])])
    trace = res.pin_trace(c.pin)
    out.iss_trace = trace
    em = res.ems[0]
    flags_bad = em.LATE or ("UNF" in c.never and em.UNF) or ("HALT" in c.never and em.halted)
    # contract on the concrete ISS trace: pad changes become events in order
    events = [Event(z3.IntVal(t), c.pin, z3.BoolVal(bool(v)), (0, i), -1)
              for i, (t, v) in enumerate(trace)]
    bytes_ = [Byte(z3.BitVecVal(d, 8), z3.IntVal(a)) for d, a in zip(m["data"], m["arrival"])]
    prop, frames, defs = contract_formula(c, events, z3.IntVal(m["P"]), bytes_)
    chk = z3.Solver()
    chk.add(*defs, prop)
    ok = chk.check() == z3.sat           # everything is concrete: sat iff the trace complies
    out.iss_confirms = bool(flags_bad or not ok)
    if not ok:
        out.first_mismatch = _first_mismatch(c, trace, res.initial[c.pin], m)


def _first_mismatch(c: Contract, trace, initial, m):
    """Walk the expected waveform for each frame (started where the ISS started it) and
    return the first cycle at which the ISS pin differs."""
    def lvl(t):
        v = initial
        for tt, vv in trace:
            if tt > t:
                break
            v = vv
        return v

    idle = c.idle_level
    prev_end = c.idle_from
    P = m["P"]
    for d, a in zip(m["data"], m["arrival"]):
        starts = [t for t, v in trace if v != idle and t >= prev_end]
        if not starts:
            return (max(prev_end, a) + c.start_within, 1 - idle, idle)
        s = starts[0]
        for seg in expand_waveform(c.waveform):
            env = {"P": P, "d": d, "k": seg.get("_k", 0)}
            bit = lambda dd, i: (dd >> i) & 1
            f = eval_expr(seg["from"], env, bit)
            t = eval_expr(seg["to"], env, bit)
            want = int(eval_expr(seg["level"], env, bit))
            for cyc in sorted({s + f} | {tt for tt, _ in trace if s + f < tt < s + t}):
                if lvl(cyc) != want:
                    return (cyc, want, lvl(cyc))
        prev_end = s + eval_expr(c.length, {"P": P}, None)
    return None


def prove_file(program_path, contract_path=None, P_range=None) -> Outcome:
    from .contract import contract_for
    cpath = Path(contract_path) if contract_path else contract_for(program_path)
    c = load_contract(cpath)
    words = assemble(Path(program_path).read_text(), str(program_path))
    return prove(c, words, P_range)


def min_P(c: Contract, words, lo: int = 1) -> int:
    """Smallest m such that the contract is proved for every P in m..P_max."""
    m = lo
    while not prove(c, words, (m, c.P_max)).proved:
        m += 1
    return m
