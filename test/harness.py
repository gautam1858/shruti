# SPDX-FileCopyrightText: 2026 Gautam Ramachandra
# SPDX-License-Identifier: Apache-2.0
"""Cycle-level cocotb harness for Shruti: a host SPI master, pin stimulus and a pad recorder,
all advanced by one tick per core clock so every event has an exact cycle number.

Cycle numbering: tick k is the clock period after the k-th rising edge. Inputs set for tick k
are what the chip samples at the edge that ends it; outputs read in tick k are the register
values during it. That is the ISS convention, so an ISS cycle c maps to tick start + c.
"""

import os
import sys
from pathlib import Path

from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GATES = os.environ.get("GATES") == "yes"

# host register map (src/project.v)
EM_BASE = (0x80, 0x90)
CTRL, FLAGS, P_LO, P_HI, CFG_OUT, CFG_IN, TXPORT, RXPORT, RXCNT, PC = range(10)
SYNC, FILTER, RELEASE, LEVELS, ID, VERSION, RUN_ALL = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6

# Cycles from the SCK rising edge of the last bit of a register write to the first cycle in
# which the write is in effect (two synchroniser flops, edge detect, the write strobe).
WRITE_LATENCY = 4


class Chip:
    def __init__(self, dut, half=4):
        self.dut = dut
        self.half = half            # SCK half period in core cycles (SCK = clk / 8)
        self.k = 0
        self.ext = 0xFF             # B pins: external level (pulled up)
        self.m = 0xF                # M0-M3
        self.sck, self.mosi, self.cs = 0, 0, 1
        self.stim = {}              # tick -> [(pin, level)]
        self.pads = {}              # tick -> 12-bit pad levels, while recording
        self.recording = False
        self.last_rise = None
        self.probe = None          # called every tick in the read-only phase
        self.rst_n = 0

    async def reset(self):
        self.rst_n = 0
        self.ext, self.m = 0xFF, 0xF
        self.sck, self.mosi, self.cs = 0, 0, 1
        self.stim.clear()
        self.pads.clear()
        self.recording = False
        await self.ticks(5)
        self.rst_n = 1
        await self.ticks(5)

    async def start_clock(self):
        from cocotb import start_soon
        start_soon(Clock(self.dut.clk, 20, unit="ns").start())

    async def tick(self):
        await RisingEdge(self.dut.clk)
        self.k += 1
        for pin, level in self.stim.pop(self.k, []):
            if pin < 8:
                self.ext = (self.ext & ~(1 << pin)) | (level << pin)
            else:
                self.m = (self.m & ~(1 << (pin - 8))) | (level << (pin - 8))
        self.dut.ena.value = 1
        self.dut.rst_n.value = self.rst_n
        self.dut.ext.value = self.ext
        self.dut.ui_in.value = (self.m << 4) | (self.cs << 2) | (self.mosi << 1) | self.sck
        await ReadOnly()
        uo = self.dut.uo_out.value
        uo = int(uo) if uo.is_resolvable else 0      # X before the first reset
        self.miso = uo & 1
        self.irq = (uo >> 1) & 1
        if self.recording:
            self.pads[self.k] = int(self.dut.uio_in.value) | (self.m << 8)
        if self.probe is not None:
            self.probe()

    async def ticks(self, n):
        for _ in range(n):
            await self.tick()

    # -- host SPI (mode 0, MSB first) ---------------------------------------------------

    async def xfer(self, data):
        self.cs = 0
        await self.ticks(self.half)
        got = []
        self.first_rises = []                      # tick of each byte's first SCK rise
        for byte in data:
            v = 0
            for i in range(7, -1, -1):
                self.sck, self.mosi = 0, (byte >> i) & 1
                await self.ticks(self.half)
                v = (v << 1) | self.miso           # sampled just before the rising edge
                self.sck = 1
                await self.tick()
                self.last_rise = self.k
                if i == 7:
                    self.first_rises.append(self.k)
                await self.ticks(self.half - 1)
            got.append(v)
        self.sck = 0
        await self.ticks(self.half)
        self.cs = 1
        await self.ticks(2 * self.half)
        return got

    async def write(self, addr, data):
        await self.xfer([0x00, addr] + list(data))

    async def read(self, addr, n=1):
        return (await self.xfer([0x80, addr] + [0] * n))[2:]

    async def load(self, em, words):
        words = list(words) + [0] * (32 - len(words))
        data = []
        for w in words:
            data += [w & 0xFF, w >> 8]
        await self.write(0x40 * em, data)

    async def write_reg(self, em, reg, value):
        await self.write(EM_BASE[em] + reg, [value])

    async def read_reg(self, em, reg):
        return (await self.read(EM_BASE[em] + reg))[0]

    async def setup_em(self, em, words, P=0, cfg=None, tx=()):
        await self.load(em, words)
        await self.write(EM_BASE[em] + P_LO, [P & 0xFF, P >> 8])
        if cfg:
            out = (cfg.get("AUTOPULL", 0) << 5) | (cfg.get("OUT_MSB", 0) << 4) | cfg.get("OUT_N", 7)
            inn = (cfg.get("AUTOPUSH", 0) << 5) | (cfg.get("IN_MSB", 0) << 4) | cfg.get("IN_N", 7)
            await self.write(EM_BASE[em] + CFG_OUT, [out, inn])
        for b in tx:
            await self.write(EM_BASE[em] + TXPORT, [b])

    async def start(self, mask):
        """Start the EMs in `mask` in the same cycle; return that cycle (ISS cycle 0)."""
        self.recording = True
        await self.write(RUN_ALL, [mask])
        return self.last_rise + WRITE_LATENCY

    def schedule(self, start, stimulus):
        """ISS-style stimulus (cycle, pin, level) relative to `start`."""
        for c, pin, level in stimulus:
            assert start + c > self.k, "stimulus scheduled in the past"
            self.stim.setdefault(start + c, []).append((pin, level))

    def pad_trace(self, start, end, pins=range(8)):
        """(cycle, pin, level) changes relative to start, as the ISS reports them."""
        out = []
        prev = self.pads[start]
        for k in range(start + 1, end):
            cur = self.pads[k]
            for p in pins:
                if (cur ^ prev) >> p & 1:
                    out.append((k - start, p, (cur >> p) & 1))
            prev = cur
        return out
