"""UART transmit from the spec, run on the ISS: exact bit timing for 256 random (byte, B)."""

import random

import pytest

from iss import simulate

from .progs import uart_tx

TX = 0
FIRST_START = 5     # SET@0, SHIFT@1, PULL@2, ADDT 2,now@3 -> T = 5, OUT@4 arms the start bit


def expected_trace(frames, B, start=FIRST_START):
    """Pad changes for back-to-back 8N1 frames starting at `start`. The line starts low
    (external level) and SET drives it high at cycle 1."""
    trace, level = [(1, 1)], 1
    for k, byte in enumerate(frames):
        s = start + k * 10 * B
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
        for i, v in enumerate(bits):
            if v != level:
                trace.append((s + i * B, v))
                level = v
    return trace


def run_uart(frames, B, arrivals=None):
    arrivals = arrivals or [0] * len(frames)
    return simulate(uart_tx(TX), 1 << 40, P=B, initial={TX: 0},
                    host_tx=list(zip(arrivals, frames)))


def _pairs():
    rng = random.Random(0x5A5A)
    pairs = [(rng.randrange(256), rng.randint(8, 65535)) for _ in range(252)]
    return pairs + [(0x00, 8), (0xFF, 8), (0x55, 65535), (0xAA, 5208)]


@pytest.mark.parametrize("byte,B", _pairs())
def test_uart_tx_exact_timing(byte, B):
    res = run_uart([byte], B)
    assert res.pin_trace(TX) == expected_trace([byte], B)
    em = res.ems[0]
    assert not em.LATE and not em.UNF
    assert em.PC == 2 and em.blk is not None      # back in PULL, waiting for the next byte


@pytest.mark.parametrize("B", [4, 8, 13, 5208, 65535])
def test_uart_tx_back_to_back_frames(B):
    frames = [0x00, 0xFF, 0x5A, 0xC3, 0x81, 0x7E]
    res = run_uart(frames, B)
    assert res.pin_trace(TX) == expected_trace(frames, B)
    assert not res.ems[0].LATE


def test_uart_tx_after_idle_start_bit_follows_the_byte():
    # The second byte arrives long after the first frame ends: PULL returns at the arrival
    # cycle a, ADDT 2,now at a+1 sets T = a+3, and the start bit lands there.
    B, a = 100, 50_000
    res = run_uart([0x0F, 0xF0], B, arrivals=[0, a])
    first = expected_trace([0x0F], B)
    second = expected_trace([0xF0], B, start=a + 3)[1:]
    assert res.pin_trace(TX) == first + second


def test_uart_tx_minimum_bit_period():
    """Measured, not assumed: B = 4 keeps exact back-to-back timing; B = 2 is late."""
    frames = [0xA3, 0x3A]
    assert run_uart(frames, 4).pin_trace(TX) == expected_trace(frames, 4)
    assert run_uart(frames, 2).ems[0].LATE


def test_uart_tx_300_baud_with_three_steps_of_P():
    """Rates below 763 baud at 50 MHz: P = period / 3 and three ADDT P per bit."""
    from .progs import ADDTP, ADDT, JMP, OUT, PULL, SETPIN, SHIFT
    step3 = [ADDTP(0), ADDTP(0), ADDTP(0)]
    prog = ([SETPIN(TX, 1), SHIFT("out", "lsb", 8), PULL(), ADDT(2, now=1), OUT(TX, 0)]
            + step3 + [OUT(TX, "OSR"), JMP(5, "flag", "!OSRE")]
            + step3 + [OUT(TX, 1)] + step3 + [JMP(2)])
    assert len(prog) <= 32
    P = 55_556
    res = simulate(prog, 1 << 40, P=P, initial={TX: 0}, host_tx=[(0, 0x5A)])
    assert res.pin_trace(TX) == expected_trace([0x5A], 3 * P)
    assert not res.ems[0].LATE
