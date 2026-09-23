# SPDX-FileCopyrightText: 2026 Gautam Ramachandra
# SPDX-License-Identifier: Apache-2.0
"""Constrained-random Event Machine programs and stimulus for differential testing.

Programs are encoded from isa/isa.yaml through iss.isa, so every word decodes in strict mode.
Operands are biased towards values that make progress in a few hundred cycles (short delays,
small timeouts, small loop counts), with a small share of illegal operands that must halt.
"""

from iss.isa import load_isa

ISA = load_isa()


def _instr(rng):
    r = rng.random()
    e = ISA.encode
    illegal = rng.random() < 0.01
    if r < 0.11:
        pin = rng.randrange(12, 16) if illegal else rng.choice([0, 1, 2, 3, 8, 9, rng.randrange(12)])
        mode = rng.randrange(5, 8) if illegal and rng.random() < 0.5 else rng.randrange(5)
        tmo = rng.choice([0, 1, 2, 3, 3, 4, 4, 5, 6])
        return e("WAIT", pin=pin, mode=mode, timeout=tmo)
    if r < 0.28:
        pin = rng.randrange(8, 16) if illegal else rng.randrange(8)
        return e("OUT", pin=pin, value=rng.randrange(4), d=rng.choice([0, 1, 2, 3, 5, 9, rng.randrange(64)]))
    if r < 0.36:
        pin = rng.randrange(12, 16) if illegal else rng.randrange(12)
        return e("IN", pin=pin, snap=rng.randrange(2), d=rng.choice([0, 1, 2, 4, rng.randrange(64)]))
    if r < 0.46:
        return e("ADDT", now=rng.randrange(2), d=rng.choice([0, 1, 2, 3, 5, 8, 13, rng.randrange(40)]))
    if r < 0.53:
        return e("ADDTP", now=rng.randrange(2), shift=rng.randrange(4))
    if r < 0.59:
        return e("SHIFT", dir=rng.randrange(2), msb=rng.randrange(2), auto=rng.randrange(2),
                 n=rng.choice([7, 7, 3, 0, 15, rng.randrange(16)]))
    if r < 0.64:
        return e("PUSH", block=int(rng.random() < 0.3))
    if r < 0.69:
        return e("PULL", block=int(rng.random() < 0.3))
    if r < 0.79:
        t = rng.randrange(4)
        if t == 0:
            pin = rng.randrange(8, 16) if illegal else rng.randrange(8)
            return e("SET", target=0, pin=pin, level=rng.randrange(2), od=rng.randrange(2))
        return e("SET", target=t, imm=rng.choice([0, 1, 2, 3, 5, 7, 12, rng.randrange(40)]))
    if r < 0.93:
        cond = rng.randrange(5, 8) if illegal else rng.randrange(5)
        if cond == 0:
            sel = rng.randrange(5, 16) if illegal else rng.randrange(5)
        elif cond in (3, 4):
            sel = rng.randrange(12, 16) if illegal else rng.randrange(12)
        else:
            sel = 0
        return e("JMP", cond=cond, pin=sel, target=rng.randrange(32))
    if r < 0.99:
        return e("SYNC", op=int(rng.random() < 0.15), flag=rng.randrange(4))
    return rng.choice([0x0000, 0xC000 | rng.randrange(1 << 12), 0xF000])   # HALT, opcodes 12-15


def program(rng, n=32):
    return [_instr(rng) for _ in range(n)]


def stimulus(rng, cycles, pins=(0, 1, 2, 3, 8, 9, 10, 11), density=0.02):
    """Random external level changes (cycle, pin, level), starting at cycle 20."""
    level = {p: 1 for p in range(12)}
    out = []
    for c in range(20, cycles):
        for p in pins:
            if rng.random() < density:
                level[p] ^= 1
                out.append((c, p, level[p]))
    return out
