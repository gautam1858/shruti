"""The event-driven run loop gives exactly the same result as stepping every cycle."""

import random

import pytest

from iss import Sim

from .progs import (ADDT, ADDTP, IN, JMP, OUT, PULL, PUSH, SETPIN, SETR, SHIFT, WAIT,
                    uart_tx)


def both(programs, cycles, **kw):
    a = Sim(programs, log=True, **kw).run(cycles)
    b = Sim(programs, log=True, step_every_cycle=True, **kw).run(cycles)
    return a, b


def same(a, b):
    assert a.trace == b.trace
    assert a.log == b.log
    assert a.host_rx == b.host_rx
    for x, y in zip(a.ems, b.ems):
        for f in ("PC", "T", "T_PASSED", "X", "Y", "OSR", "OSR_COUNT", "ISR", "ISR_COUNT",
                  "SNAPSHOT", "TO", "LATE", "OVF", "UNF", "halted"):
            assert getattr(x, f) == getattr(y, f), f


@pytest.mark.parametrize("seed", range(12))
def test_uart_tx_skipping_matches_reference(seed):
    rng = random.Random(seed)
    B = rng.randint(2, 40)
    tx = [(rng.randrange(0, 400), rng.randrange(256)) for _ in range(3)]
    a, b = both([uart_tx(0)], 3000, P=[B], host_tx=[tx], initial={0: 0})
    same(a, b)


def echo_program():
    # Wait for edges on M0, sample B1 at a fixed offset, report bytes, drive B2 in response.
    return [
        SHIFT("in", "lsb", 4, auto=1),
        WAIT(8, "any", 7),
        JMP(6, "flag", "TO"),
        IN(1, 3),
        OUT(2, "1", 5),
        JMP(1),
        SETPIN(2, 0),
        ADDTP(1, now=1),
        OUT(3, "0"),
        JMP(1),
    ]


@pytest.mark.parametrize("seed", range(12))
def test_reactive_program_skipping_matches_reference(seed):
    rng = random.Random(100 + seed)
    stim, t = [], 0
    for _ in range(40):
        t += rng.randint(1, 60)
        stim.append((t, rng.choice([1, 8]), rng.randint(0, 1)))
    a, b = both([echo_program()], 3000, stimulus=stim, P=[rng.randint(1, 50)])
    same(a, b)
