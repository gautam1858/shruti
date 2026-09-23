# SPDX-FileCopyrightText: 2026 Gautam Ramachandra
# SPDX-License-Identifier: Apache-2.0
#
# Milestone 0 tests: synchroniser, selectable glitch filter, edge strobes.

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, ReadOnly

BYPASS, MAJ_2OF3, MAJ_3OF5 = 0b00, 0b01, 0b10


async def reset(dut, mode):
    dut.ena.value = 1
    dut.ui_in.value = mode
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 10)


async def count_strobes(dut, pin, cycles):
    """Count one-cycle strobes on uo_out[pin] over the next `cycles` clocks."""
    n = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if (int(dut.uo_out.value) >> pin) & 1:
            n += 1
    return n


async def glitch(dut, pin, level_after):
    """Drive pin to the opposite level for exactly one clock, then back."""
    dut.uio_in.value = (1 << pin) if not level_after else 0
    await ClockCycles(dut.clk, 1)
    dut.uio_in.value = (1 << pin) if level_after else 0


@cocotb.test()
async def clean_edge_in_3of5_mode(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, MAJ_3OF5)
    assert int(dut.uo_out.value) == 0, "no strobes while the bus is idle"
    dut.uio_in.value = 1 << 3
    assert await count_strobes(dut, 3, 12) == 1, "one clean rising edge gives exactly one strobe"
    assert await count_strobes(dut, 3, 12) == 0, "a steady level gives no further strobes"


@cocotb.test()
async def one_cycle_glitch_is_rejected_in_3of5_mode(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, MAJ_3OF5)
    dut.uio_in.value = 1 << 3
    await ClockCycles(dut.clk, 12)
    await glitch(dut, 3, level_after=1)
    assert await count_strobes(dut, 3, 12) == 0, "3-of-5 majority swallows a one-cycle glitch"


@cocotb.test()
async def one_cycle_glitch_passes_in_bypass_mode(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, BYPASS)
    dut.uio_in.value = 1 << 3
    await ClockCycles(dut.clk, 12)
    await glitch(dut, 3, level_after=1)
    assert await count_strobes(dut, 3, 12) == 2, "bypass reports both edges of the glitch"


@cocotb.test()
async def pins_are_independent(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, MAJ_2OF3)
    dut.uio_in.value = 1 << 6
    strobes_6 = 0
    strobes_other = 0
    for _ in range(12):
        await RisingEdge(dut.clk)
        await ReadOnly()
        v = int(dut.uo_out.value)
        strobes_6 += (v >> 6) & 1
        strobes_other += bin(v & ~(1 << 6)).count("1")
    assert strobes_6 == 1 and strobes_other == 0


# Differential test: the RTL input path against the Python reference model in ear/inpath.py,
# cycle by cycle, for every filter mode and random pulse widths on all eight pins.

import random  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ear.inpath import filter_cycles  # noqa: E402


def random_levels(rng, n):
    out, level = [], 0
    while len(out) < n:
        out += [level] * rng.choice([1, 1, 2, 2, 3, 4, 5, 6, 8, 13])
        level ^= 1
    return out[:n]


@cocotb.test()
async def input_path_matches_reference_model(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    rng = random.Random(2026)
    n = 800
    for mode in (BYPASS, MAJ_2OF3, MAJ_3OF5):
        await reset(dut, mode)
        u = [random_levels(rng, n) for _ in range(8)]
        observed = []
        for k in range(n):
            await RisingEdge(dut.clk)
            dut.uio_in.value = sum(u[p][k] << p for p in range(8))
            await ReadOnly()
            observed.append(int(dut.uo_out.value))
        for p in range(8):
            _, strobe = filter_cycles(u[p], mode, before=0)
            got = [(o >> p) & 1 for o in observed]
            assert got == strobe, f"mode {mode}, pin {p}: first mismatch at cycle " \
                f"{next(i for i, (a, b) in enumerate(zip(got, strobe)) if a != b)}"
        await RisingEdge(dut.clk)              # leave the read-only phase before the next reset
