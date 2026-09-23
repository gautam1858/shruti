"""Symbolic execution of Event Machine programs.

A second implementation of the instruction semantics in isa/isa.yaml, with the same timing
model as the ISS (iss/sim.py), in which times are Z3 integers and data are Z3 bit-vectors.

What stays concrete: the PC, X, Y, OSR_COUNT, the shift configuration and which FIFO byte is
next. What is symbolic: the bit period P, the data bytes, their arrival cycles, and every time
derived from them (the current cycle, T, slot fire times, pin drive times).

Times are unbounded integers rather than 24-bit counter values. That is exact as long as every
future time the program computes stays less than 2^23 cycles ahead of the counter, which is an
obligation the executor emits alongside the LATE checks.

v0 scope: programs whose control flow does not depend on symbolic values (no WAIT, IN, JMP on a
pin, TO or TXE). Anything else raises Unsupported rather than guessing.
"""

from __future__ import annotations

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
    pc: int                                    # instruction that produced it


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
class SymResult:
    events: List[Event]
    obligations: List[Obligation]
    stopped_at: int                            # PC where exploration ended
    steps: int
    end_time: z3.ArithRef


@dataclass
class _Slot:
    fire: z3.ArithRef


def _max(a, b):
    return z3.If(a >= b, a, b)


def _min(a, b):
    return z3.If(a <= b, a, b)


class SymExec:
    def __init__(self, program: Sequence[int], P: z3.ArithRef, tx: Sequence[Byte],
                 cfg: Optional[Dict[str, int]] = None, max_steps: int = 2000):
        self.words = list(program) + [0] * (ISA.program_words - len(program))
        self.P = P
        self.tx = list(tx)
        self.max_steps = max_steps
        self.cfg = {"OUT_MSB": 0, "OUT_N": 7, "IN_MSB": 0, "IN_N": 7, "AUTOPULL": 0,
                    "AUTOPUSH": 0, **(cfg or {})}

    def run(self) -> SymResult:
        now: z3.ArithRef = z3.IntVal(0)
        T: z3.ArithRef = z3.IntVal(0)
        pc, X, Y = 0, 0, 0
        cfg = dict(self.cfg)
        osr: Optional[z3.BitVecRef] = None          # 16-bit symbolic OSR
        osr_count = cfg["OUT_N"] + 1                # empty
        next_byte = 0
        slots: List[Optional[_Slot]] = [None, None]
        P_override: Optional[int] = None
        events: List[Event] = []
        obl: List[Obligation] = []
        seq = 0
        steps = 0

        def write_T(v, at, pc_):
            obl.append(Obligation("T within 2^23 cycles of the counter", v - at < HALF, pc_))
            return v

        def pull(at, pc_):
            nonlocal osr, osr_count, next_byte
            n = 1 if cfg["OUT_N"] + 1 <= 8 else 2
            if next_byte + n > len(self.tx):
                return None                                  # blocks forever: stop
            bs = self.tx[next_byte: next_byte + n]
            next_byte += n
            done = at
            for b in bs:
                done = _max(done, b.arrival)
            osr = z3.ZeroExt(8, bs[0].data) if n == 1 else z3.Concat(bs[1].data, bs[0].data)
            osr_count = 0
            return done

        while True:
            steps += 1
            if steps > self.max_steps:
                raise Unsupported(f"no end after {self.max_steps} instructions")
            ins = ISA.decode(self.words[pc])
            o = ins.ops
            name = ins.name
            nxt = pc + 1

            if name == "HALT":
                obl.append(Obligation("never HALT", z3.BoolVal(False), pc))
                return SymResult(events, obl, pc, steps, now)

            elif name == "SET":
                t = o["target"]
                if t == 0:
                    if o["od"]:
                        raise Unsupported("open-drain pins")
                    if o["pin"] >= ISA.output_pins:
                        obl.append(Obligation("never HALT", z3.BoolVal(False), pc))
                        return SymResult(events, obl, pc, steps, now)
                    seq += 1
                    events.append(Event(now + 1, o["pin"], z3.BoolVal(bool(o["level"])),
                                        (1, seq), pc))
                elif t == 1:
                    X = o["imm"]
                elif t == 2:
                    Y = o["imm"]
                else:
                    P_override = o["imm"]

            elif name == "SHIFT":
                if o["dir"] == 0:
                    cfg.update(OUT_MSB=o["msb"], OUT_N=o["n"], AUTOPULL=o["auto"])
                    osr_count = o["n"] + 1
                else:
                    cfg.update(IN_MSB=o["msb"], IN_N=o["n"], AUTOPUSH=o["auto"])

            elif name == "PULL":
                n = 1 if cfg["OUT_N"] + 1 <= 8 else 2
                if not o["block"]:
                    raise Unsupported("non-blocking PULL (depends on symbolic arrival times)")
                done = pull(now, pc)
                if done is None:
                    return SymResult(events, obl, pc, steps, now)
                now = done

            elif name in ("ADDT", "ADDTP"):
                if name == "ADDT":
                    delta = z3.IntVal(o["d"])
                else:
                    base = z3.IntVal(P_override) if P_override is not None else self.P
                    delta = base / (1 << o["shift"]) if o["shift"] else base
                v = T + delta if not o["now"] else _max(T, now + delta)
                T = write_T(v, now, pc)

            elif name == "OUT":
                pin, val, d = o["pin"], o["value"], o["d"]
                if pin >= ISA.output_pins:
                    obl.append(Obligation("never HALT", z3.BoolVal(False), pc))
                    return SymResult(events, obl, pc, steps, now)
                w = cfg["OUT_N"] + 1
                if val == 2 and osr_count >= w and cfg["AUTOPULL"]:
                    done = pull(now, pc)
                    if done is None:
                        return SymResult(events, obl, pc, steps, now)
                    now = done
                # wait for a free slot: a slot is free from the cycle it fires
                fires = [s.fire for s in slots if s is not None]
                if len(fires) == 2:
                    arm = _max(now, _min(fires[0], fires[1]))
                    victim = z3.If(fires[0] <= fires[1], 0, 1)
                else:
                    arm, victim = now, None
                if val in (0, 1):
                    level = z3.BoolVal(bool(val))
                elif osr_count >= w:
                    obl.append(Obligation("never UNF", z3.BoolVal(False), pc))
                    level = z3.BoolVal(val == 3)
                else:
                    idx = osr_count if not cfg["OUT_MSB"] else cfg["OUT_N"] - osr_count
                    bit = z3.Extract(idx, idx, osr) == 1
                    level = bit if val == 2 else z3.Not(bit)
                    if val == 2:
                        osr_count += 1
                fire = T + d
                obl.append(Obligation("never LATE (OUT on time)",
                                      z3.And(fire - arm >= 1, fire - arm < HALF), pc))
                seq += 1
                events.append(Event(fire, pin, level, (0, seq), pc))
                new = _Slot(fire)
                if victim is None:
                    slots[slots.index(None)] = new
                else:
                    a, b = slots
                    slots = [_Slot(z3.If(victim == 0, fire, a.fire)),
                             _Slot(z3.If(victim == 0, b.fire, fire))]
                now = arm

            elif name == "JMP":
                cond, sel, tgt = o["cond"], o["pin"], o["target"]
                if cond == 0 and sel == 0:
                    take = True
                elif cond == 0 and sel == 4:
                    take = osr_count < cfg["OUT_N"] + 1
                elif cond == 0 and sel == 2:
                    raise Unsupported("JMP LATE (the contract already forbids LATE)")
                elif cond in (1, 2):
                    reg = X if cond == 1 else Y
                    take = reg != 0
                    if take:
                        if cond == 1:
                            X -= 1
                        else:
                            Y -= 1
                else:
                    raise Unsupported(f"JMP on a symbolic condition (cond={cond}, sel={sel})")
                if take:
                    nxt = tgt

            elif name == "SYNC" and o["op"] == 0:
                pass

            else:
                raise Unsupported(f"{name} is not supported by prover v0")

            now = now + 1
            pc = nxt % ISA.program_words
