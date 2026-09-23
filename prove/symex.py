"""Symbolic execution of Event Machine programs.

A second implementation of the instruction semantics in isa/isa.yaml, with the same timing
model as the ISS (iss/sim.py), in which times are Z3 integers and data are Z3 bit-vectors.

What stays concrete: the PC, X, Y, OSR_COUNT, ISR_COUNT, the shift configuration and which
FIFO byte is next. What is symbolic: the bit period P, the data bytes and their arrival
cycles, input pin waveforms, and every time derived from them (the current cycle, T, slot fire
times, pin drive times, sampled input levels).

Inputs. A pin can be given an input waveform (Wave): its pad level as a sequence of
segments with symbolic start times and symbolic levels. WAIT on such a pin forks over which
segment boundary it matches; JMP HIGH/LOW forks on the level; IN samples it. The program sees
a pad change L cycles later, as in the ISS. A pin can instead be marked deferred: IN returns a
fresh variable and records (variable, pin, sample time), and the contract later ties the
variable to a peer model that may depend on the program's own outputs (SPI MISO, say).

Paths. Branches on symbolic values fork the state; each fork is kept only if its path
condition is satisfiable under the caller's assumptions. run() returns the single path of a
program without symbolic branches; paths() returns every feasible path.

Times are unbounded integers rather than 24-bit counter values. That is exact as long as every
future time the program computes stays less than 2^23 cycles ahead of the counter, which is an
obligation the executor emits alongside the LATE checks.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import z3

from iss.isa import load_isa

ISA = load_isa()
HALF = 1 << (ISA.counter_bits - 1)


class Unsupported(Exception):
    pass


@dataclass
class Event:
    """A drive of a pin: from `time` on, the pin's output register holds `level`.
    Events at the same time are applied in `key` order (slots before SET, then arm order)."""

    time: z3.ArithRef
    pin: int
    level: z3.BoolRef
    key: Tuple[int, int]
    pc: int                                    # instruction that produced it (-1: initial)
    od: bool = False                           # open-drain: level True releases the pin


@dataclass
class Obligation:
    name: str
    cond: z3.BoolRef
    pc: int


@dataclass
class Byte:
    data: z3.BitVecRef                         # 8 bits
    arrival: z3.ArithRef                       # cycle the host writes it


@dataclass
class Wave:
    """Pad level of an input pin: `initial` before the first boundary, then levels[i] from
    times[i] on. Times must be non-decreasing under the caller's assumptions."""

    initial: z3.BoolRef
    times: List[z3.ArithRef]
    levels: List[z3.BoolRef]

    def level_at(self, t) -> z3.BoolRef:
        lvl = self.initial
        for tt, v in zip(self.times, self.levels):
            lvl = z3.If(tt <= t, v, lvl)
        return lvl

    def boundaries(self) -> List[z3.ArithRef]:
        return list(self.times)


@dataclass
class AndSignal:
    """Wired-AND of two signals (an open-drain line driven by two devices)."""

    a: object
    b: object

    def level_at(self, t) -> z3.BoolRef:
        return z3.And(self.a.level_at(t), self.b.level_at(t))

    def boundaries(self) -> List[z3.ArithRef]:
        return self.a.boundaries() + self.b.boundaries()


@dataclass
class SymResult:
    events: List[Event]
    obligations: List[Obligation]
    stopped_at: int                            # PC where exploration ended
    steps: int
    end_time: z3.ArithRef
    path: List[z3.BoolRef] = field(default_factory=list)
    pushed: List[Tuple[z3.ArithRef, z3.BitVecRef]] = field(default_factory=list)
    sync_set: List[Tuple[int, z3.ArithRef]] = field(default_factory=list)
    deferred: List[Tuple[z3.BoolRef, int, z3.ArithRef]] = field(default_factory=list)
    final: dict = field(default_factory=dict)
    reason: str = ""                           # why exploration ended


def _max(a, b):
    return z3.If(a >= b, a, b)


def _min(a, b):
    return z3.If(a <= b, a, b)


@dataclass
class _State:
    pc: int
    now: z3.ArithRef
    T: z3.ArithRef
    X: int = 0
    Y: int = 0
    P_override: Optional[int] = None
    cfg: dict = field(default_factory=dict)
    osr: Optional[z3.BitVecRef] = None
    osr_count: int = 8
    isr: List[Optional[z3.BoolRef]] = field(default_factory=lambda: [None] * 16)
    isr_count: int = 0
    next_byte: int = 0
    slots: List[Optional[z3.ArithRef]] = field(default_factory=lambda: [None, None])
    snapshot: Dict[int, z3.BoolRef] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    obl: List[Obligation] = field(default_factory=list)
    path: List[z3.BoolRef] = field(default_factory=list)
    pushed: list = field(default_factory=list)
    sync_set: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    visits: Dict[int, int] = field(default_factory=dict)
    od: Dict[int, bool] = field(default_factory=dict)
    seq: int = 0
    steps: int = 0


class SymExec:
    def __init__(self, program: Sequence[int], P: z3.ArithRef, tx: Sequence[Byte],
                 cfg: Optional[Dict[str, int]] = None, max_steps: int = 4000,
                 inputs: Optional[Dict[int, Wave]] = None, deferred: Sequence[int] = (),
                 latency: int = 2, init: Optional[dict] = None,
                 stop_at: Optional[Tuple[int, int]] = None):
        self.words = list(program) + [0] * (ISA.program_words - len(program))
        self.P = P
        self.tx = list(tx)
        self.max_steps = max_steps
        self.inputs = dict(inputs or {})
        self.deferred_pins = set(deferred)
        self.L = latency
        self.stop_at = stop_at                 # (pc, visit number): stop on that visit
        base_cfg = {"OUT_MSB": 0, "OUT_N": 7, "IN_MSB": 0, "IN_N": 7, "AUTOPULL": 0,
                    "AUTOPUSH": 0, **(cfg or {})}
        s = _State(pc=0, now=z3.IntVal(0), T=z3.IntVal(0), cfg=base_cfg,
                   osr_count=base_cfg["OUT_N"] + 1)
        if init:                               # start from an abstract state (induction steps)
            s.pc = init.get("pc", 0)
            s.now = init.get("now", s.now)
            s.T = init.get("T", s.T)
            s.osr_count = init.get("osr_count", s.osr_count)
            for fire, pin, level in init.get("slots", []):
                s.slots[s.slots.index(None)] = fire
                s.seq += 1
                s.events.append(Event(fire, pin, level, (0, -100 + s.seq), -1))
        self.initial_state = s

    # -- public ------------------------------------------------------------------------

    def run(self) -> SymResult:
        """The single path of a program without symbolic branches."""
        out = self._explore(None)
        if len(out) != 1:
            raise Unsupported("program branches on symbolic values: use paths()")
        return out[0]

    def paths(self, assumptions: Sequence[z3.BoolRef], on_path=None) -> List[SymResult]:
        """Every path feasible under the assumptions. If on_path is given it is called with
        each finished path, and exploration stops as soon as it returns True."""
        return self._explore(list(assumptions), on_path)

    # -- exploration ---------------------------------------------------------------------

    def _explore(self, assumptions, on_path=None) -> List[SymResult]:
        solver = None
        if assumptions is not None:
            solver = z3.Solver()
            solver.add(*assumptions)
        work = [copy.deepcopy(self.initial_state)]
        done: List[SymResult] = []
        while work:
            s = work.pop()
            while True:
                res = self._step(s)
                if isinstance(res, SymResult):
                    done.append(res)
                    if on_path is not None and on_path(res):
                        return done
                    break
                if res is None:
                    continue
                # a fork: keep the feasible successors
                succ = []
                for label, st in res:
                    if solver is None:
                        raise Unsupported("program branches on symbolic values: use paths()")
                    solver.push()
                    solver.add(*st.path)
                    ok = solver.check() == z3.sat
                    solver.pop()
                    if not ok:
                        continue
                    if label == "stuck":
                        done.append(self._end(st, "WAIT never matches"))
                        if on_path is not None and on_path(done[-1]):
                            return done
                    else:
                        succ.append(st)
                if not succ:
                    break
                s = succ[0]
                work.extend(succ[1:])
        return done

    def _end(self, s: _State, reason: str) -> SymResult:
        final = dict(pc=s.pc, now=s.now, T=s.T, slots=list(s.slots), osr_count=s.osr_count,
                     X=s.X, Y=s.Y, isr_count=s.isr_count)
        return SymResult(s.events, s.obl, s.pc, s.steps, s.now, list(s.path), list(s.pushed),
                         list(s.sync_set), list(s.deferred), final, reason)

    def _fork(self, s: _State, cond: z3.BoolRef):
        """Two copies of s: one with cond added to the path, one with its negation."""
        simp = z3.simplify(cond)
        if z3.is_true(simp):
            return [(True, s)]
        if z3.is_false(simp):
            return [(False, s)]
        a, b = copy.deepcopy(s), copy.deepcopy(s)
        a.path.append(cond)
        b.path.append(z3.Not(cond))
        return [(True, a), (False, b)]

    # -- inputs ------------------------------------------------------------------------

    def _signal(self, pin: int, s: "_State"):
        if pin not in self.inputs:
            raise Unsupported(f"reading pin {pin}, which has no input waveform")
        src = self.inputs[pin]
        return src(s) if callable(src) else src

    def _visible(self, pin: int, t, s: "_State" = None) -> z3.BoolRef:
        return self._signal(pin, s).level_at(t - self.L)

    # -- one instruction -------------------------------------------------------------------

    def _step(self, s: _State):
        """Execute one instruction in place. Returns None to continue, a SymResult when the
        path ends, or a list of (label, state) forks whose execution continues."""
        s.steps += 1
        if s.steps > self.max_steps:
            raise Unsupported(f"no end after {self.max_steps} instructions")
        if self.stop_at is not None:
            s.visits[s.pc] = s.visits.get(s.pc, 0) + 1
            if (s.pc, s.visits[s.pc]) == self.stop_at:
                return self._end(s, "stop_at")
        ins = ISA.decode(self.words[s.pc])
        o = ins.ops
        name = ins.name
        cfg = s.cfg
        nxt = s.pc + 1

        def halt():
            s.obl.append(Obligation("never HALT", z3.BoolVal(False), s.pc))
            return self._end(s, "HALT")

        def write_T(v):
            s.obl.append(Obligation("T within 2^23 cycles of the counter", v - s.now < HALF, s.pc))
            s.T = v

        def pull():
            n = 1 if cfg["OUT_N"] + 1 <= 8 else 2
            if s.next_byte + n > len(self.tx):
                return None                                  # blocks forever: stop
            bs = self.tx[s.next_byte: s.next_byte + n]
            s.next_byte += n
            done = s.now
            for b in bs:
                done = _max(done, b.arrival)
            s.osr = z3.ZeroExt(8, bs[0].data) if n == 1 else z3.Concat(bs[1].data, bs[0].data)
            s.osr_count = 0
            return done

        def push_word():
            w = cfg["IN_N"] + 1
            bits = [s.isr[i] if s.isr[i] is not None else z3.BoolVal(False) for i in range(16)]
            val = z3.Concat(*[z3.If(b, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
                              for b in reversed(bits)])
            s.pushed.append((s.now, z3.Extract(w - 1, 0, val) if w < 16 else val))
            s.isr = [None] * 16
            s.isr_count = 0

        if name == "HALT":
            return halt()

        elif name == "SET":
            t = o["target"]
            if t == 0:
                if o["pin"] >= ISA.output_pins:
                    return halt()
                s.od[o["pin"]] = bool(o["od"])
                s.seq += 1
                s.events.append(Event(s.now + 1, o["pin"], z3.BoolVal(bool(o["level"])),
                                      (1, s.seq), s.pc, bool(o["od"])))
            elif t == 1:
                s.X = o["imm"]
            elif t == 2:
                s.Y = o["imm"]
            else:
                s.P_override = o["imm"]

        elif name == "SHIFT":
            if o["dir"] == 0:
                cfg.update(OUT_MSB=o["msb"], OUT_N=o["n"], AUTOPULL=o["auto"])
                s.osr_count = o["n"] + 1
            else:
                cfg.update(IN_MSB=o["msb"], IN_N=o["n"], AUTOPUSH=o["auto"])
                s.isr = [None] * 16
                s.isr_count = 0

        elif name == "PULL":
            if not o["block"]:
                n = 1 if cfg["OUT_N"] + 1 <= 8 else 2
                if s.next_byte + n > len(self.tx):
                    s.now = s.now + 1                        # nothing more will ever arrive
                    s.pc = nxt % ISA.program_words
                    return None
                ready = z3.And(*[b.arrival <= s.now
                                 for b in self.tx[s.next_byte: s.next_byte + n]])
                out = []
                for got, st in self._fork(s, ready):
                    if got:
                        bs = self.tx[st.next_byte: st.next_byte + n]
                        st.next_byte += n
                        st.osr = (z3.ZeroExt(8, bs[0].data) if n == 1
                                  else z3.Concat(bs[1].data, bs[0].data))
                        st.osr_count = 0
                    st.now = st.now + 1
                    st.pc = nxt % ISA.program_words
                    out.append((got, st))
                return out
            done = pull()
            if done is None:
                return self._end(s, "PULL with no more bytes")
            s.now = done

        elif name == "PUSH":
            push_word()                                       # host drains: never blocks

        elif name in ("ADDT", "ADDTP"):
            if name == "ADDT":
                delta = z3.IntVal(o["d"])
            else:
                base = z3.IntVal(s.P_override) if s.P_override is not None else self.P
                delta = base / (1 << o["shift"]) if o["shift"] else base
            write_T(s.T + delta if not o["now"] else _max(s.T, s.now + delta))

        elif name == "OUT":
            pin, val, d = o["pin"], o["value"], o["d"]
            if pin >= ISA.output_pins:
                return halt()
            w = cfg["OUT_N"] + 1
            if val == 2 and s.osr_count >= w and cfg["AUTOPULL"]:
                done = pull()
                if done is None:
                    return self._end(s, "autopull with no more bytes")
                s.now = done
            fires = [f for f in s.slots if f is not None]
            if len(fires) == 2:
                arm = _max(s.now, _min(fires[0], fires[1]))
                victim = z3.If(fires[0] <= fires[1], 0, 1)
            else:
                arm, victim = s.now, None
            if val in (0, 1):
                level = z3.BoolVal(bool(val))
            elif s.osr_count >= w:
                s.obl.append(Obligation("never UNF", z3.BoolVal(False), s.pc))
                level = z3.BoolVal(val == 3)
            else:
                idx = s.osr_count if not cfg["OUT_MSB"] else cfg["OUT_N"] - s.osr_count
                bit = z3.Extract(idx, idx, s.osr) == 1
                level = bit if val == 2 else z3.Not(bit)
                if val == 2:
                    s.osr_count += 1
            fire = s.T + d
            s.obl.append(Obligation("never LATE (OUT on time)",
                                    z3.And(fire - arm >= 1, fire - arm < HALF), s.pc))
            s.seq += 1
            s.events.append(Event(fire, pin, level, (0, s.seq), s.pc, s.od.get(pin, False)))
            if victim is None:
                s.slots[s.slots.index(None)] = fire
            else:
                a, b = s.slots
                s.slots = [z3.If(victim == 0, fire, a), z3.If(victim == 0, b, fire)]
            s.now = arm

        elif name == "IN":
            pin, snap, d = o["pin"], o["snap"], o["d"]
            if pin >= ISA.pins:
                return halt()
            if snap:
                if pin not in s.snapshot:
                    raise Unsupported("IN @snap before any WAIT matched")
                bit = s.snapshot[pin]
            else:
                target = s.T + d
                s.obl.append(Obligation("never LATE (IN on time)",
                                        z3.And(target - s.now >= 0, target - s.now < HALF), s.pc))
                s.now = _max(s.now, target)
                if pin in self.deferred_pins:
                    bit = z3.Bool(f"in_{pin}_{len(s.deferred)}")
                    s.deferred.append((bit, pin, s.now))
                else:
                    bit = self._visible(pin, s.now, s)
            w = cfg["IN_N"] + 1
            if s.isr_count < w:
                idx = s.isr_count if not cfg["IN_MSB"] else cfg["IN_N"] - s.isr_count
                s.isr[idx] = bit
                s.isr_count += 1
                if s.isr_count == w and cfg["AUTOPUSH"]:
                    push_word()

        elif name == "WAIT":
            pin, mode, tmo = o["pin"], o["mode"], o["timeout"]
            if pin >= ISA.pins or mode > 4 or tmo > 23:
                return halt()
            if tmo:
                raise Unsupported("WAIT with a timeout")
            if pin not in self.inputs:
                raise Unsupported(f"WAIT on pin {pin}, which has no input waveform")
            sig = self._signal(pin, s)
            w = s.now
            L = self.L
            cands = []                                   # (visible time, condition)
            if mode in (3, 4):
                lvl_w = sig.level_at(w - L)
                cands.append((w, lvl_w if mode == 3 else z3.Not(lvl_w)))
            if hasattr(sig, "split_boundaries"):
                older, recent = sig.split_boundaries()
                if older:                                # checked, not assumed
                    s.obl.append(Obligation("older edges were visible before the WAIT",
                                            z3.And(*[ob + L < w for ob in older]), s.pc))
            else:
                recent = sig.boundaries()
            for b in recent:
                now_l, before = sig.level_at(b), sig.level_at(b - 1)
                if mode == 0:
                    cond = z3.And(now_l, z3.Not(before))
                elif mode == 1:
                    cond = z3.And(z3.Not(now_l), before)
                elif mode == 2:
                    cond = now_l != before
                elif mode == 3:
                    cond = z3.And(now_l, z3.Not(before))
                else:
                    cond = z3.And(z3.Not(now_l), before)
                cands.append((b + L, z3.And(b + L >= w, cond)))
            # fork: candidate i is the earliest match (ties: the first listed)
            forks, none_before = [], []
            for i, (t_m, cond) in enumerate(cands):
                earlier = [z3.Not(z3.And(cj, z3.Or(tj < t_m, z3.And(tj == t_m, j < i))))
                           for j, (tj, cj) in enumerate(cands) if j != i]
                st = copy.deepcopy(s)
                st.path.append(z3.And(cond, *earlier))
                st.T = t_m
                st.now = t_m + 1
                st.snapshot = {p: self._visible(p, t_m, s) for p in self.inputs}
                st.pc = (s.pc + 1) % ISA.program_words
                forks.append(("match", st))
                none_before.append(z3.Not(cond))
            stuck = copy.deepcopy(s)
            stuck.path.append(z3.And(*none_before) if none_before else z3.BoolVal(True))
            stuck.reason = "WAIT never matches"
            forks.append(("stuck", stuck))
            return self._wait_forks(forks)

        elif name == "JMP":
            cond, sel, tgt = o["cond"], o["pin"], o["target"]
            if cond == 0 and sel == 0:
                take = True
            elif cond == 0 and sel == 4:
                take = s.osr_count < cfg["OUT_N"] + 1
            elif cond == 0 and sel == 2:
                raise Unsupported("JMP LATE (the contract already forbids LATE)")
            elif cond in (1, 2):
                reg = s.X if cond == 1 else s.Y
                take = reg != 0
                if take:
                    if cond == 1:
                        s.X -= 1
                    else:
                        s.Y -= 1
            elif cond in (3, 4):
                if sel >= ISA.pins:
                    return halt()
                lvl = self._visible(sel, s.now, s)
                c = lvl if cond == 3 else z3.Not(lvl)
                out = []
                for taken, st in self._fork(s, c):
                    st.pc = tgt if taken else (st.pc + 1) % ISA.program_words
                    st.now = st.now + 1
                    out.append((taken, st))
                return out
            else:
                raise Unsupported(f"JMP on a symbolic condition (cond={cond}, sel={sel})")
            if take:
                nxt = tgt

        elif name == "SYNC":
            if o["op"] == 0:
                s.sync_set.append((o["flag"], s.now))
            else:
                raise Unsupported("SYNC wait")

        else:
            raise Unsupported(f"{name} is not supported by the prover")

        s.now = s.now + 1
        s.pc = nxt % ISA.program_words
        return None

    def _wait_forks(self, forks):
        return forks
