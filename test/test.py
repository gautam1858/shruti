# SPDX-FileCopyrightText: 2026 Gautam Ramachandra
# SPDX-License-Identifier: Apache-2.0
#
# Milestone 1 tests: the host SPI, the Event Machines against the ISS (iss/sim.py), the
# UART transmitter firmware out of a pin, and the input filter. Every test runs at RTL and
# on the gate-level netlist; the ones that look inside the design say so and run at RTL only.

import random

import cocotb

from harness import (CFG_OUT, CTRL, EM_BASE, FILTER, FLAGS, GATES, ID, P_LO, RUN_ALL, RXPORT,
                     SYNC, TXPORT, Chip)
import randprog

import fw
from fw.peers import uart_decode
from iss import Sim


async def new_chip(dut):
    chip = Chip(dut)
    await chip.start_clock()
    await chip.reset()
    return chip


# -- host interface ------------------------------------------------------------------------

@cocotb.test()
async def host_spi_registers(dut):
    chip = await new_chip(dut)
    assert await chip.read(ID, 2) == [0x53, 0x11], "ID and ISA version"
    await chip.write(EM_BASE[0] + P_LO, [0x34, 0x12])
    await chip.write(EM_BASE[1] + P_LO, [0xCD, 0xAB])
    assert await chip.read(EM_BASE[0] + P_LO, 2) == [0x34, 0x12]
    assert await chip.read(EM_BASE[1] + P_LO, 2) == [0xCD, 0xAB]
    await chip.write(EM_BASE[0] + CFG_OUT, [0x3F, 0x10])
    assert await chip.read(EM_BASE[0] + CFG_OUT, 2) == [0x3F, 0x10]
    await chip.write(SYNC, [0x5])
    await chip.write(FILTER, [0x2])
    assert await chip.read(SYNC, 2) == [0x5, 0x2]
    assert await chip.read(EM_BASE[0] + CTRL) == [0], "EMs are stopped after reset"
    assert chip.irq == 0


# -- the UART transmitter out of a pin -----------------------------------------------------

async def run_uart_tx(chip, B, data):
    words = fw.load("uart_tx")
    await chip.setup_em(0, words, P=B, tx=data[:4])
    frame = 10 * B
    cycles = 40 + frame * len(data)
    start = await chip.start(0b01)
    # the rest of the bytes go in while the first ones are on the wire, one per frame
    host_tx = [(0, b) for b in data[:4]]
    for b in data[4:]:
        await chip.ticks(max(0, start + frame * (len(host_tx) - 2) - chip.k))
        await chip.write(EM_BASE[0] + TXPORT, [b])
        host_tx.append((chip.last_rise + 4 - start, b))
    await chip.ticks(start + cycles - chip.k)
    got = chip.pad_trace(start, start + cycles, pins=[0])
    ref = Sim([words], P=[B], host_tx=[host_tx], rx_drain=False).run(cycles)
    assert got == ref.trace, "pin B0 differs from the ISS"
    frames = uart_decode([(c, v) for c, _, v in got], 1, B, cycles)
    assert [b for _, b, _ in frames] == data and all(ok for _, _, ok in frames)
    assert (await chip.read_reg(0, FLAGS)) == 0, "no LATE, OVF or UNF"


@cocotb.test()
async def uart_tx_out_of_a_pin(dut):
    """fw/uart_tx.s on EM0 sends bytes on B0, matching the ISS cycle for cycle."""
    chip = await new_chip(dut)
    rng = random.Random(1)
    for B in ([16, 50] if not GATES else [16]):
        await chip.reset()
        await run_uart_tx(chip, B, [rng.randrange(256) for _ in range(6)])


# -- Event Machines against the ISS --------------------------------------------------------

def probe_em(dut, em):
    u = getattr(dut.user_project, f"u_em{em}")
    g = lambda s: int(getattr(u, s).value)
    return {
        "PC": g("pc"), "T": g("T"), "X": g("X"), "Y": g("Y"), "P": g("P"),
        "OSR": g("osr"), "OSR_COUNT": g("osr_cnt"), "ISR": g("isr"), "ISR_COUNT": g("isr_cnt"),
        "SNAPSHOT": g("snapshot"), "PIN_MODE": g("pin_mode"),
        "TO": g("f_to"), "LATE": g("f_late"), "OVF": g("f_ovf"), "UNF": g("f_unf"),
        "halted": g("halted"), "tx": g("tx_cnt"), "rx": g("rx_cnt"),
        "OUT_N": g("out_n"), "OUT_MSB": g("out_msb"), "AUTOPULL": g("autopull"),
        "IN_N": g("in_n"), "IN_MSB": g("in_msb"), "AUTOPUSH": g("autopush"),
        "txd": [g(f"txm{(g('tx_rd') + i) % 4}") for i in range(g("tx_cnt"))],
        "rxd": [g(f"rxm{(g('rx_rd') + i) % 4}") for i in range(g("rx_cnt"))],
    }


def iss_em(em):
    return {
        "PC": em.PC, "T": em.T, "X": em.X, "Y": em.Y, "P": em.P,
        "OSR": em.OSR, "OSR_COUNT": em.OSR_COUNT, "ISR": em.ISR, "ISR_COUNT": em.ISR_COUNT,
        "SNAPSHOT": em.SNAPSHOT, "PIN_MODE": em.PIN_MODE,
        "TO": int(em.TO), "LATE": int(em.LATE), "OVF": int(em.OVF), "UNF": int(em.UNF),
        "halted": int(em.halted), "tx": len(em.tx), "rx": len(em.rx),
        "OUT_N": em.OUT_N, "OUT_MSB": em.OUT_MSB, "AUTOPULL": em.AUTOPULL,
        "IN_N": em.IN_N, "IN_MSB": em.IN_MSB, "AUTOPUSH": em.AUTOPUSH,
        "txd": list(em.tx), "rxd": list(em.rx),
    }


async def differential_case(chip, rng, cycles, per_cycle):
    progs = [randprog.program(rng), randprog.program(rng)]
    Ps = [rng.choice([0, 1, 3, 5, 8, 13, 40]) for _ in range(2)]
    cfgs = [{"OUT_N": rng.choice([7, 3, 15, 0]), "OUT_MSB": rng.randrange(2),
             "AUTOPULL": rng.randrange(2), "IN_N": rng.choice([7, 3, 15, 0]),
             "IN_MSB": rng.randrange(2), "AUTOPUSH": rng.randrange(2)} for _ in range(2)]
    txs = [[rng.randrange(256) for _ in range(rng.randrange(5))] for _ in range(2)]
    stim = randprog.stimulus(rng, cycles)

    await chip.reset()
    for em in range(2):
        await chip.setup_em(em, progs[em], P=Ps[em], cfg=cfgs[em], tx=txs[em])
    probes = {}
    if per_cycle:
        dut = chip.dut

        def probe():
            probes[chip.k] = (probe_em(dut, 0), probe_em(dut, 1), int(dut.user_project.sync.value),
                              int(dut.user_project.cnt.value))
        chip.probe = probe
    start = await chip.start(0b11)
    chip.schedule(start, stim)
    # mid-run host writes: a TX FIFO byte or all four SYNC flags. A write in effect from
    # cycle w is an ISS host action at w.
    host_tx = [[(0, b) for b in t] for t in txs]
    host_sync = []
    host_rx_pop = []
    for _ in range(rng.randrange(4)):
        await chip.ticks(rng.randrange(60))
        r = rng.random()
        if r < 0.3:
            # an RX FIFO read pops each byte when its first bit is clocked
            em, nb = rng.randrange(2), rng.randrange(1, 3)
            await chip.read(EM_BASE[em] + RXPORT, nb)
            host_rx_pop += [(t + 4 - start, em) for t in chip.first_rises[2:]]
        elif r < 0.7:
            em, b = rng.randrange(2), rng.randrange(256)
            await chip.write(EM_BASE[em] + TXPORT, [b])
            host_tx[em].append((chip.last_rise + 4 - start, b))
        else:
            v = rng.randrange(16)
            await chip.write(SYNC, [v])
            host_sync += [(chip.last_rise + 4 - start, f, (v >> f) & 1) for f in range(4)]
    await chip.ticks(max(0, start + cycles - chip.k))
    await chip.write(RUN_ALL, [0])
    stop = chip.last_rise + 4                     # first cycle with both EMs stopped
    chip.probe = None
    n = stop - start                              # ISS cycles 0 .. n-1 ran on the chip

    offset = probes[start][3] if per_cycle else 0   # counter value in ISS cycle 0
    sim = Sim(progs, stimulus=stim, P=Ps, cfg=cfgs, host_tx=host_tx, host_sync=host_sync,
              host_tx_drop=True, host_rx_pop=host_rx_pop, rx_drain=False, step_every_cycle=True, counter_offset=offset)
    where = f"host_tx {host_tx} sync {host_sync} pops {host_rx_pop} P {Ps} cfg {cfgs}"
    landing = ({w for h in host_tx for w, _ in h} | {w for w, _, _ in host_sync}
               | {w for w, _ in host_rx_pop})
    if per_cycle:
        for c in range(n):
            sim.run(c + 1)
            got = probes[start + c + 1][:3]
            want = (iss_em(sim.ems[0]), iss_em(sim.ems[1]), sum(f << i for i, f in enumerate(sim.sync)))
            if c + 1 in landing:
                # the RTL register already holds a host write the ISS applies in cycle c + 1
                host = {"tx": 0, "txd": 0, "rx": 0, "rxd": 0}
                got = ({**got[0], **host}, {**got[1], **host}, 0)
                want = ({**want[0], **host}, {**want[1], **host}, 0)
            if got != want:
                for em in range(2):
                    diff = {k: (got[em][k], want[em][k]) for k in want[em] if got[em][k] != want[em][k]}
                    if diff:
                        raise AssertionError(f"cycle {c}, EM{em} (rtl, iss): {diff}; {where}")
                raise AssertionError(f"cycle {c}: SYNC rtl {got[2]} iss {want[2]}; {where}")
    else:
        sim.run(n)
    got = chip.pad_trace(start, stop)
    want = [e for e in sim.trace if e[0] < n and e[1] < 8]
    assert got == want, f"pin trace differs; first rtl/iss: {got[:4]} / {want[:4]}; {where}"
    for em in range(2):
        rx = await chip.read(EM_BASE[em] + RXPORT, len(sim.ems[em].rx)) if sim.ems[em].rx else []
        assert rx == sim.ems[em].rx, f"EM{em} RX FIFO"
    return n


@cocotb.test()
async def event_machines_match_the_iss(dut):
    """Random two-EM programs and random pin stimulus: pins, RX FIFOs and (at RTL) every
    architectural register, cycle by cycle, against the ISS."""
    chip = await new_chip(dut)
    chip.probe = None
    rng = random.Random(2026)
    cases = 6 if GATES else 60
    for _ in range(cases):
        await differential_case(chip, rng, 400, per_cycle=not GATES)


async def pull_while_full(chip, n):
    """EM0 pulls one byte at cycle n + 2 from a full TX FIFO while the host writes one more.
    Returns the cycle the write takes effect and the TX FIFO count afterwards."""
    from asm import assemble
    prog = assemble(f"SET X, {n}\nw: JMP X--, w\nPULL\nHALT")
    await chip.reset()
    await chip.setup_em(0, prog, tx=[1, 2, 3, 4])
    start = await chip.start(0b01)
    await chip.write(EM_BASE[0] + TXPORT, [9])
    w = chip.last_rise + 4 - start
    await chip.ticks(max(0, start + n + 10 - chip.k))
    return w, await chip.read_reg(0, TXPORT)


@cocotb.test()
async def tx_fifo_write_in_the_cycle_of_a_pull(dut):
    """A host byte written to a full TX FIFO is dropped, unless the EM pulls in that very
    cycle; the ISS (host_tx_drop) agrees at the cycle before, at and after the pull."""
    from asm import assemble
    chip = await new_chip(dut)
    w, _ = await pull_while_full(chip, 1000)
    counts = {}
    for n in (w - 4, w - 3, w - 2):
        w2, counts[n] = await pull_while_full(chip, n)
        assert w2 == w
        prog = assemble(f"SET X, {n}\nw: JMP X--, w\nPULL\nHALT")
        ref = Sim([prog], host_tx=[[(0, b) for b in (1, 2, 3, 4)] + [(w, 9)]], host_tx_drop=True,
                  rx_drain=False).run(n + 20)
        assert counts[n] == len(ref.ems[0].tx), f"PULL at cycle {n + 2}, write at {w}"
    assert counts[w - 3] == 4, "the write in the cycle of the pull is kept"
    assert counts[w - 2] == 3, "a write one cycle before the pull is dropped"


RX_WRAP = """
        WAIT  M0, high           ; latch the snapshot: M0 = 1, M1 = 0
        SET   X, 7
a:      IN    M0 @snap
        JMP   X--, a
        PUSH                     ; 0xFF (8-bit words)
        SHIFT in, lsb, 16
        SET   X, 7
b:      IN    M0 @snap
        JMP   X--, b
        SET   X, 7
c:      IN    M1 @snap
        JMP   X--, c
        PUSH  block              ; 0xFF, 0x00: the FIFO holds three bytes
        SET   X, 7
d:      IN    M0 @snap
        JMP   X--, d
        SET   X, 7
e:      IN    M1 @snap
        JMP   X--, e
        PUSH  block              ; waits for the host to make room, then wraps around
        HALT
"""


@cocotb.test()
async def rx_fifo_two_byte_push_wraps(dut):
    """A 16-bit PUSH whose two bytes straddle the end of the RX FIFO ring."""
    from asm import assemble
    chip = await new_chip(dut)
    chip.m = 0b1101                                  # M1 low
    prog = assemble(RX_WRAP)
    await chip.setup_em(0, prog)
    start = await chip.start(0b01)
    await chip.ticks(start + 80 - chip.k)
    first = await chip.read(EM_BASE[0] + RXPORT, 2)
    pops = [(t + 4 - start, 0) for t in chip.first_rises[2:]]
    await chip.ticks(100)
    rest = await chip.read(EM_BASE[0] + RXPORT, 3)
    end = chip.first_rises[2] + 4 - start
    ref = Sim([prog], initial={9: 0}, rx_drain=False, host_rx_pop=pops).run(end)
    assert first == ref.host_rx[0] == [0xFF, 0xFF]
    assert rest == ref.ems[0].rx == [0x00, 0xFF, 0x00]


# -- input filter, seen through an Event Machine ---------------------------------------------

MIRROR = """
.pin src M0
.pin dst B0
top:  WAIT src, low
      SET  dst, 0
      WAIT src, high
      SET  dst, 1
      JMP  top
"""


async def mirror_edges(chip, mode, pulses):
    """Mirror M0 onto B0 with EM0 and count B0 edges for the given M0 pulse widths."""
    from asm import assemble
    await chip.reset()
    await chip.write(FILTER, [mode])
    await chip.setup_em(0, assemble(MIRROR))
    start = await chip.start(0b01)
    t, stim = 40, []
    for w in pulses:
        stim += [(t, 8, 0), (t + w, 8, 1)]
        t += w + 30
    chip.schedule(start, stim)
    await chip.ticks(start + t + 20 - chip.k)
    return len(chip.pad_trace(start, start + t + 20, pins=[0]))


@cocotb.test()
async def glitch_filter_modes(dut):
    chip = await new_chip(dut)
    # EM0 copies every low pulse on M0 that survives the filter onto B0 (two B0 edges each):
    # bypass passes a one-cycle glitch, 2-of-3 needs two cycles, 3-of-5 three
    assert await mirror_edges(chip, 0, [1]) == 2
    assert await mirror_edges(chip, 1, [1]) == 0
    assert await mirror_edges(chip, 1, [2]) == 2
    assert await mirror_edges(chip, 2, [2]) == 0
    assert await mirror_edges(chip, 2, [3, 8]) == 4


# -- the input path against its bit-exact reference (RTL only: it reads filt_d) -----------------

from ear.inpath import filter_cycles  # noqa: E402


def random_levels(rng, n):
    out, level = [], 0
    while len(out) < n:
        out += [level] * rng.choice([1, 1, 2, 2, 3, 4, 5, 6, 8, 13])
        level ^= 1
    return out[:n]


@cocotb.test(skip=GATES)
async def input_path_matches_reference_model(dut):
    chip = await new_chip(dut)
    rng = random.Random(2026)
    n = 600
    for mode in (0, 1, 2):
        await chip.reset()
        chip.ext, chip.m = 0, 0
        await chip.write(FILTER, [mode])
        await chip.ticks(10)
        u = [random_levels(rng, n) for _ in range(12)]
        base = chip.k + 1
        for k in range(n):
            for p in range(12):
                if k == 0 or u[p][k] != u[p][k - 1]:
                    chip.stim.setdefault(base + k, []).append((p, u[p][k]))
        observed = {}
        chip.probe = lambda: observed.__setitem__(chip.k, int(chip.dut.user_project.filt_d.value))
        await chip.ticks(n + 1)
        chip.probe = None
        for p in range(12):
            filt, _ = filter_cycles(u[p], mode, before=0)
            got = [(observed[base + k] >> p) & 1 for k in range(n)]
            assert got == filt, f"mode {mode}, pin {p}: first mismatch at cycle " \
                f"{next(i for i, (a, b) in enumerate(zip(got, filt)) if a != b)}"
