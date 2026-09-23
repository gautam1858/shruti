"""Cycle-accurate instruction-set simulator for Shruti Event Machines.

Models one or two Event Machines and the parts they share: the free-running 24-bit timestamp
counter, input synchronisation with edge capture and the edge-latched 12-pin snapshot, two
scheduler slots per EM, 4-deep TX and RX FIFOs per EM, the four SYNC flags, and pin drive in
push-pull and open-drain modes. Instruction semantics follow the timing model in isa/isa.yaml
exactly; decode comes from iss.isa, which is generated from the same file.

The simulator is event-driven but cycle-exact: when every EM is blocked it jumps straight to
the next cycle at which anything can change (a stimulus edge, an input becoming visible, a
slot firing, a deadline, a host action), so bit periods of 65,535 cycles cost no more to
simulate than bit periods of 8.

Order of events within cycle c:
  1. External stimulus and device-scheduled changes effective at c; scheduler slots whose time
     is c fire (EM0 then EM1, earlier-armed first, so the later-armed value wins); SET drives
     issued at c-1 take effect. Pad levels for cycle c are resolved.
  2. T_PASSED is set for any EM whose T equals the counter.
  3. The host acts: SYNC flag writes, TX FIFO writes, RX FIFO drain.
  4. Attached peer devices observe the pads at c and may schedule external changes for c+1 on.
  5. Each EM executes (or continues) one instruction, EM0 first.
Inputs seen by the EMs at cycle c are the pad levels at cycle c - L (synchroniser latency).
"""

from __future__ import annotations

import bisect
import heapq
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .isa import ISA, Instr, load_isa

NEG_INF = -(1 << 62)


class ProgramError(Exception):
    pass


@dataclass
class Slot:
    busy: bool = False
    fire_abs: int = 0      # absolute cycle at which the slot takes effect
    time: int = 0          # 24-bit compare value, as armed
    pin: int = 0
    value: int = 0
    seq: int = 0           # arm order, for "later-armed wins"


@dataclass
class EMState:
    """Architectural state of one Event Machine (names follow isa.yaml)."""

    program: List[int]
    PC: int = 0
    T: int = 0
    T_PASSED: bool = True
    X: int = 0
    Y: int = 0
    P: int = 0
    OSR: int = 0
    OSR_COUNT: int = 8        # empty for the reset word length of 8
    ISR: int = 0
    ISR_COUNT: int = 0
    SNAPSHOT: int = 0
    OUT_MSB: int = 0
    OUT_N: int = 7            # word length minus 1; reset word length 8
    AUTOPULL: int = 0
    IN_MSB: int = 0
    IN_N: int = 7
    AUTOPUSH: int = 0
    TO: bool = False
    LATE: bool = False
    OVF: bool = False
    UNF: bool = False
    PIN_MODE: int = 0         # bit per B pin: 1 = open-drain
    halted: bool = False
    halt_reason: str = ""
    irq: bool = False
    slots: List[Slot] = field(default_factory=lambda: [Slot(), Slot()])
    tx: List[int] = field(default_factory=list)   # host -> EM
    rx: List[int] = field(default_factory=list)   # EM -> host
    blk: Optional[dict] = None                    # progress of a blocked instruction
    retired: int = 0                              # instructions completed

    @property
    def out_w(self) -> int:
        return self.OUT_N + 1

    @property
    def in_w(self) -> int:
        return self.IN_N + 1

    @property
    def osr_empty(self) -> bool:
        return self.OSR_COUNT >= self.out_w


@dataclass
class Result:
    cycles: int                                   # first cycle not simulated
    trace: List[Tuple[int, int, int]]             # (cycle, pin, level) pad changes
    initial: List[int]                            # pad levels before cycle 0
    ems: List[EMState]
    host_rx: List[List[int]]                      # bytes the host drained, per EM
    sync: List[int]
    log: Optional[List[Tuple[int, int, int, str]]] = None

    def level_at(self, pin: int, cycle: int) -> int:
        level = self.initial[pin]
        for c, p, v in self.trace:
            if c > cycle:
                break
            if p == pin:
                level = v
        return level

    def pin_trace(self, pin: int) -> List[Tuple[int, int]]:
        return [(c, v) for (c, p, v) in self.trace if p == pin]

    @property
    def rx_fifo(self) -> List[List[int]]:
        return [list(em.rx) for em in self.ems]

    @property
    def tx_fifo(self) -> List[List[int]]:
        return [list(em.tx) for em in self.ems]


class Device:
    """A peer device model. Subclasses override on_cycle and next_event."""

    def attach(self, sim: "Sim") -> None:
        self.sim = sim

    def on_cycle(self, c: int) -> None:
        """Called at every evaluated cycle, after pads for c are resolved."""

    def next_event(self, c: int) -> Optional[int]:
        """The next cycle > c at which on_cycle must run even if nothing else happens."""
        return None


class Sim:
    def __init__(
        self,
        programs: Sequence[Sequence[int]],
        *,
        stimulus: Iterable[Tuple[int, int, int]] = (),
        initial: Optional[Dict[int, int]] = None,
        latency: int = 2,
        P: Optional[Sequence[int]] = None,
        host_tx: Sequence[Iterable[Tuple[int, int]]] = (),
        host_sync: Iterable[Tuple[int, int, int]] = (),
        rx_drain: bool = True,
        devices: Iterable[Device] = (),
        counter_offset: int = 0,
        strict: bool = True,
        log: bool = False,
        step_every_cycle: bool = False,
        isa: Optional[ISA] = None,
    ):
        self.isa = isa or load_isa()
        if not 1 <= len(programs) <= 2:
            raise ProgramError("one or two Event Machines")
        self.M = (1 << self.isa.counter_bits) - 1
        self.HALF = 1 << (self.isa.counter_bits - 1)
        self.L = latency
        self.strict = strict
        self.offset = counter_offset
        self.ems: List[EMState] = []
        for i, prog in enumerate(programs):
            prog = list(prog)
            if len(prog) > self.isa.program_words:
                raise ProgramError(f"EM{i}: {len(prog)} words > {self.isa.program_words}")
            for w in prog:
                if not 0 <= w <= 0xFFFF:
                    raise ProgramError(f"EM{i}: word {w} out of range")
            prog += [0] * (self.isa.program_words - len(prog))
            em = EMState(program=prog)
            em.T = self.counter(0)
            if P is not None:
                em.P = P[i] & 0xFFFF
            self.ems.append(em)
        self.decoded = [[self.isa.decode(w, strict) for w in em.program] for em in self.ems]

        npins = self.isa.pins
        init = initial or {}
        self.ext = [init.get(p, 1) for p in range(npins)]
        self.oe = [0] * npins
        self.out = [0] * npins
        self.pad = list(self.ext)
        self.initial = list(self.pad)
        self._log_t: List[List[int]] = [[NEG_INF] for _ in range(npins)]
        self._log_v: List[List[int]] = [[self.pad[p]] for p in range(npins)]
        self.trace: List[Tuple[int, int, int]] = []

        stim = sorted(stimulus, key=lambda e: e[0])         # stable: same-cycle order kept
        for c, p, v in stim:
            if c < 0 or not 0 <= p < npins or v not in (0, 1):
                raise ProgramError(f"bad stimulus edge {(c, p, v)}")
        self._ext_q: List[Tuple[int, int, int, int]] = []   # (cycle, seq, pin, level)
        self._seq = 0
        for c, p, v in stim:
            self._push_ext(c, p, v)
        self._set_q: List[Tuple[int, int, int, int, int]] = []  # (cycle, em, pin, level, od)
        self._vis_q: List[int] = []

        self.host_tx = [sorted(h, key=lambda e: e[0]) for h in host_tx] + [[] for _ in range(2)]
        self.host_tx = self.host_tx[: len(self.ems)]
        self.host_sync = sorted(host_sync, key=lambda e: e[0])  # (cycle, flag, value)
        self.rx_drain = rx_drain
        self.host_rx: List[List[int]] = [[] for _ in self.ems]
        self.sync = [0] * self.isa.sync_flags
        self._arm_seq = 0
        self.devices = list(devices)
        for d in self.devices:
            d.attach(self)
        self.c = 0
        self.log: Optional[List[Tuple[int, int, int, str]]] = [] if log else None
        self.step_every_cycle = step_every_cycle   # reference mode: no event skipping

    # -- helpers ----------------------------------------------------------------------

    def counter(self, c: int) -> int:
        return (c + self.offset) & self.M

    def future(self, t: int, c: int) -> bool:
        return 1 <= ((t - self.counter(c)) & self.M) < self.HALF

    def _push_ext(self, c: int, p: int, v: int) -> None:
        heapq.heappush(self._ext_q, (c, self._seq, p, v))
        self._seq += 1

    def set_external(self, pin: int, level: int, at: int) -> None:
        """Schedule an external level change (for peer devices). Must be in the future."""
        if at <= self.c:
            raise ValueError(f"external change at {at} is not after cycle {self.c}")
        self._push_ext(at, pin, level)

    def pad_at(self, pin: int, cycle: int) -> int:
        i = bisect.bisect_right(self._log_t[pin], cycle) - 1
        return self._log_v[pin][i]

    def vis(self, pin: int, cycle: int) -> int:
        """Filtered, synchronised level seen by the EMs at cycle."""
        return self.pad_at(pin, cycle - self.L)

    def _drive(self, em: EMState, pin: int, level: int) -> None:
        if (em.PIN_MODE >> pin) & 1:
            self.oe[pin], self.out[pin] = (1, 0) if level == 0 else (0, 0)
        else:
            self.oe[pin], self.out[pin] = 1, level

    # -- one cycle --------------------------------------------------------------------

    def _step(self, c: int) -> None:
        # 1. pad-affecting events effective at c
        while self._ext_q and self._ext_q[0][0] <= c:
            cc, _, p, v = heapq.heappop(self._ext_q)
            assert cc == c, "external event skipped"
            self.ext[p] = v
        for i, em in enumerate(self.ems):
            fired = sorted((s for s in em.slots if s.busy and s.fire_abs == c), key=lambda s: s.seq)
            for s in fired:
                self._drive(em, s.pin, s.value)
                s.busy = False
            while self._set_q and self._set_q[0][0] == c and self._set_q[0][1] == i:
                _, _, pin, level, _od = heapq.heappop(self._set_q)
                self._drive(em, pin, level)
        for p in range(self.isa.pins):
            lvl = self.out[p] if (p < self.isa.output_pins and self.oe[p]) else self.ext[p]
            if lvl != self.pad[p]:
                self.pad[p] = lvl
                self._log_t[p].append(c)
                self._log_v[p].append(lvl)
                self.trace.append((c, p, lvl))
                heapq.heappush(self._vis_q, c + self.L)
        # 2. T_PASSED
        for em in self.ems:
            if em.T == self.counter(c):
                em.T_PASSED = True
        # 3. host
        while self.host_sync and self.host_sync[0][0] <= c:
            _, f, v = self.host_sync.pop(0)
            self.sync[f] = v
        for i, em in enumerate(self.ems):
            q = self.host_tx[i]
            while q and q[0][0] <= c and len(em.tx) < self.isa.fifo_depth:
                em.tx.append(q.pop(0)[1] & 0xFF)
            if self.rx_drain and em.rx:
                self.host_rx[i].extend(em.rx)
                em.rx.clear()
        # 4. devices
        for d in self.devices:
            d.on_cycle(c)
        # 5. EMs
        for i, em in enumerate(self.ems):
            if not em.halted:
                self._exec(i, em, c)

    # -- event scheduling -------------------------------------------------------------

    def _runnable(self) -> bool:
        return any(not em.halted and em.blk is None for em in self.ems)

    def _next_event(self, c: int) -> Optional[int]:
        cands: List[int] = []
        while self._vis_q and self._vis_q[0] <= c:
            heapq.heappop(self._vis_q)
        if self._vis_q:
            cands.append(self._vis_q[0])
        if self._ext_q:
            cands.append(self._ext_q[0][0])
        if self._set_q:
            cands.append(self._set_q[0][0])
        for i, em in enumerate(self.ems):
            for s in em.slots:
                if s.busy:
                    cands.append(s.fire_abs)
            if em.blk is not None and not em.halted:
                for key in ("deadline", "target"):
                    v = em.blk.get(key)
                    if v is not None and v > c:
                        cands.append(v)
            q = self.host_tx[i]
            if q and len(em.tx) < self.isa.fifo_depth:     # room only appears when the EM pulls
                cands.append(max(q[0][0], c + 1))
            if self.rx_drain and em.rx:
                cands.append(c + 1)
        if self.host_sync:
            cands.append(max(self.host_sync[0][0], c + 1))
        for d in self.devices:
            n = d.next_event(c)
            if n is not None:
                cands.append(max(n, c + 1))
        cands = [x for x in cands if x > c]
        return min(cands) if cands else None

    def _skip_to(self, c: int, n: int) -> None:
        """Account for cycles c+1 .. n-1, in which nothing changes."""
        span = n - (c + 1)
        if span <= 0:
            return
        for em in self.ems:
            if ((em.T - self.counter(c + 1)) & self.M) < span:
                em.T_PASSED = True

    def run(self, cycles: int) -> Result:
        """Simulate until cycle `cycles` (exclusive) or until nothing can change."""
        while self.c < cycles:
            self._step(self.c)
            if self._runnable() or self.step_every_cycle:
                nxt = self.c + 1
            else:
                nxt = self._next_event(self.c)
                if nxt is None:                      # nothing can change again
                    self._skip_to(self.c, cycles)
                    self.c = cycles
                    break
                nxt = min(nxt, cycles)
                self._skip_to(self.c, nxt)
            self.c = nxt
        return Result(self.c, list(self.trace), list(self.initial), self.ems,
                      self.host_rx, list(self.sync), self.log)

    # -- instruction semantics ------------------------------------------------------

    def _halt(self, em: EMState, reason: str) -> None:
        em.halted = True
        em.irq = True
        em.halt_reason = reason
        em.blk = None

    def _write_T(self, em: EMState, v: int, c: int) -> None:
        em.T = v & self.M
        em.T_PASSED = not self.future(em.T, c)

    def _exec(self, idx: int, em: EMState, c: int) -> None:
        ins = self.decoded[idx][em.PC]
        handler = getattr(self, "_op_" + ins.name)
        nxt = handler(idx, em, ins, c)
        if em.halted:
            if self.log is not None:
                self.log.append((c, idx, em.PC, "HALT"))
            return
        if nxt is False:
            return                                   # blocked
        em.blk = None
        em.retired += 1
        if self.log is not None:
            self.log.append((c, idx, em.PC, ins.name))    # (completion cycle, EM, PC, name)
        em.PC = (em.PC + 1) % self.isa.program_words if nxt is True else nxt

    def _op_HALT(self, idx, em, ins: Instr, c):
        if "undefined_opcode" in ins.ops:
            self._halt(em, f"undefined opcode {ins.ops['undefined_opcode']}")
        else:
            self._halt(em, "HALT")
        return False

    def _op_WAIT(self, idx, em, ins, c):
        pin, mode, tmo = ins["pin"], ins["mode"], ins["timeout"]
        if pin >= self.isa.pins or mode > 4 or tmo > 23:
            self._halt(em, "illegal WAIT operand")
            return False
        if em.blk is None:
            em.blk = {"start": c, "deadline": None}
            if tmo:
                D = (em.T + (1 << tmo)) & self.M
                em.blk["D"] = D
                em.blk["deadline"] = c + ((D - self.counter(c)) & self.M) if self.future(D, c) else c
        now, prev = self.vis(pin, c), self.vis(pin, c - 1)
        match = (
            (mode == 0 and prev == 0 and now == 1)
            or (mode == 1 and prev == 1 and now == 0)
            or (mode == 2 and prev != now)
            or (mode == 3 and now == 1)
            or (mode == 4 and now == 0)
        )
        if match:
            self._write_T(em, self.counter(c), c)
            em.SNAPSHOT = sum(self.vis(p, c) << p for p in range(self.isa.pins))
            return True
        dl = em.blk["deadline"]
        if dl is not None and c >= dl:
            em.TO = True
            self._write_T(em, em.blk["D"], c)
            return True
        return False

    def _osr_bit_index(self, em: EMState) -> int:
        return em.OSR_COUNT if not em.OUT_MSB else em.OUT_N - em.OSR_COUNT

    def _try_pull(self, em: EMState) -> bool:
        n = 1 if em.out_w <= 8 else 2
        if len(em.tx) < n:
            return False
        b0 = em.tx.pop(0)
        b1 = em.tx.pop(0) if n == 2 else 0
        em.OSR = b0 | (b1 << 8)
        em.OSR_COUNT = 0
        return True

    def _try_push(self, em: EMState) -> bool:
        n = 1 if em.in_w <= 8 else 2
        if self.isa.fifo_depth - len(em.rx) < n:
            return False
        em.rx.append(em.ISR & 0xFF)
        if n == 2:
            em.rx.append((em.ISR >> 8) & 0xFF)
        em.ISR = 0
        em.ISR_COUNT = 0
        return True

    def _op_OUT(self, idx, em, ins, c):
        pin, val, d = ins["pin"], ins["value"], ins["d"]
        if pin >= self.isa.output_pins:
            self._halt(em, "OUT on a pin that cannot be driven")
            return False
        if em.blk is None:
            em.blk = {}
        b = em.blk
        if val == 2 and em.osr_empty and em.AUTOPULL and not b.get("pulled"):
            if not self._try_pull(em):
                return False
            b["pulled"] = True
        free = [s for s in em.slots if not s.busy]
        if not free:
            return False
        if val in (0, 1):
            level = val
        elif em.osr_empty:
            em.UNF = True
            level = 0 if val == 2 else 1
        else:
            bit = (em.OSR >> self._osr_bit_index(em)) & 1
            if val == 2:
                level = bit
                em.OSR_COUNT += 1
            else:
                level = bit ^ 1
        t = (em.T + d) & self.M
        if self.future(t, c):
            fire = c + ((t - self.counter(c)) & self.M)
        else:
            em.LATE = True
            fire = c + 1
            t = self.counter(fire)
        s = free[0]
        self._arm_seq += 1
        s.busy, s.fire_abs, s.time, s.pin, s.value, s.seq = True, fire, t, pin, level, self._arm_seq
        return True

    def _isr_shift(self, em: EMState, bit: int) -> None:
        if em.ISR_COUNT >= em.in_w:
            return                                   # full, no autopush: bit dropped
        idx = em.ISR_COUNT if not em.IN_MSB else em.IN_N - em.ISR_COUNT
        em.ISR |= (bit & 1) << idx
        em.ISR_COUNT += 1

    def _op_IN(self, idx, em, ins, c):
        pin, snap, d = ins["pin"], ins["snap"], ins["d"]
        if pin >= self.isa.pins:
            self._halt(em, "illegal IN pin")
            return False
        if em.blk is None:
            em.blk = {"target": None, "sampled": False}
            if not snap:
                t = (em.T + d) & self.M
                if t == self.counter(c) or self.future(t, c):
                    em.blk["target"] = c + ((t - self.counter(c)) & self.M)
                else:
                    em.LATE = True
                    em.blk["target"] = c
        b = em.blk
        if not b["sampled"]:
            if snap:
                bit = (em.SNAPSHOT >> pin) & 1
            else:
                if c < b["target"]:
                    return False
                bit = self.vis(pin, c)
            self._isr_shift(em, bit)
            b["sampled"] = True
            b["target"] = None
        if em.AUTOPUSH and em.ISR_COUNT >= em.in_w:
            if not self._try_push(em):
                return False
        return True

    def _addt(self, em, c, delta, now):
        if not now:
            self._write_T(em, em.T + delta, c)
            return
        cand = (self.counter(c) + delta) & self.M
        if em.T_PASSED:
            self._write_T(em, cand, c)
        elif ((em.T - cand) & self.M) < self.HALF:
            self._write_T(em, em.T, c)           # keep T (it is later than counter + delta)
        else:
            self._write_T(em, cand, c)

    def _op_ADDT(self, idx, em, ins, c):
        self._addt(em, c, ins["d"], ins["now"])
        return True

    def _op_ADDTP(self, idx, em, ins, c):
        self._addt(em, c, em.P >> ins["shift"], ins["now"])
        return True

    def _op_SHIFT(self, idx, em, ins, c):
        if ins["dir"] == 0:
            em.OUT_MSB, em.AUTOPULL, em.OUT_N = ins["msb"], ins["auto"], ins["n"]
            em.OSR_COUNT = em.out_w
        else:
            em.IN_MSB, em.AUTOPUSH, em.IN_N = ins["msb"], ins["auto"], ins["n"]
            em.ISR = 0
            em.ISR_COUNT = 0
        return True

    def _op_PUSH(self, idx, em, ins, c):
        if self._try_push(em):
            return True
        if ins["block"]:
            em.blk = {}
            return False
        em.OVF = True
        em.ISR = 0
        em.ISR_COUNT = 0
        return True

    def _op_PULL(self, idx, em, ins, c):
        if self._try_pull(em):
            return True
        if ins["block"]:
            em.blk = {}
            return False
        return True

    def _op_SET(self, idx, em, ins, c):
        target = ins["target"]
        if target == 0:
            pin = ins["pin"]
            if pin >= self.isa.output_pins:
                self._halt(em, "SET on a pin that cannot be driven")
                return False
            od = ins["od"]
            em.PIN_MODE = (em.PIN_MODE & ~(1 << pin)) | (od << pin)
            heapq.heappush(self._set_q, (c + 1, idx, pin, ins["level"], od))
        elif target == 1:
            em.X = ins["imm"]
        elif target == 2:
            em.Y = ins["imm"]
        else:
            em.P = ins["imm"]
        return True

    def _op_JMP(self, idx, em, ins, c):
        cond, sel, target = ins["cond"], ins["pin"], ins["target"]
        take = False
        if cond == 0:
            if sel == 0:
                take = True
            elif sel == 1:
                take = em.TO
                em.TO = False
            elif sel == 2:
                take = em.LATE
            elif sel == 3:
                take = len(em.tx) == 0
            elif sel == 4:
                take = not em.osr_empty
            else:
                self._halt(em, "reserved JMP flag")
                return False
        elif cond in (1, 2):
            reg = "X" if cond == 1 else "Y"
            if getattr(em, reg) != 0:
                setattr(em, reg, (getattr(em, reg) - 1) & 0xFFFF)
                take = True
        elif cond in (3, 4):
            if sel >= self.isa.pins:
                self._halt(em, "illegal JMP pin")
                return False
            take = self.vis(sel, c) == (1 if cond == 3 else 0)
        else:
            self._halt(em, "reserved JMP condition")
            return False
        return target if take else True

    def _op_SYNC(self, idx, em, ins, c):
        f = ins["flag"]
        if ins["op"] == 0:
            self.sync[f] = 1
            return True
        if self.sync[f]:
            self.sync[f] = 0
            return True
        em.blk = {}
        return False


def simulate(program: Sequence[int], cycles: int, **kw) -> Result:
    """Run a single Event Machine program. Keyword arguments as for Sim; host_tx may be
    given as a flat list of (cycle, byte) for the one EM."""
    if "host_tx" in kw and kw["host_tx"] and isinstance(kw["host_tx"][0], tuple):
        kw["host_tx"] = [kw["host_tx"]]
    if "P" in kw and isinstance(kw["P"], int):
        kw["P"] = [kw["P"]]
    return Sim([program], **kw).run(cycles)
