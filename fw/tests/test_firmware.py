"""Firmware on the ISS against Python models of the peer devices."""

import random

import pytest

import fw
from fw.peers import I2cSlave, SpiSlave, i2c_timing, uart_decode, uart_stimulus
from iss import Sim, simulate

UART_B = [16, 50, 434, 5208]            # 3.125 Mbaud, 1 Mbaud, 115200, 9600 at 50 MHz


def test_word_counts_fit_the_program_memory():
    sizes = {n: len(fw.load(n)) for n in ("uart_tx", "uart_rx", "spi_master", "i2c_master")}
    assert sizes == {"uart_tx": 12, "uart_rx": 17, "spi_master": 19, "i2c_master": 32}


# -- UART TX ------------------------------------------------------------------------------

@pytest.mark.parametrize("B", UART_B)
def test_uart_tx_received_by_peer(B):
    rng = random.Random(B)
    data = [rng.randrange(256) for _ in range(6)]
    res = simulate(fw.load("uart_tx"), 1 << 40, P=B, host_tx=[(0, b) for b in data])
    frames = uart_decode(res.pin_trace(0), res.initial[0], B, res.cycles)
    assert [b for _, b, _ in frames] == data
    assert all(ok for _, _, ok in frames)
    starts = [t for t, _, _ in frames]
    assert all(b - a == 10 * B for a, b in zip(starts, starts[1:]))     # no gaps
    assert not res.ems[0].LATE


# -- UART RX ------------------------------------------------------------------------------

RX = 8          # M0


def run_rx(stim, B, cycles=None):
    end = (max(c for c, _, _ in stim) if stim else 0) + 20 * B
    return simulate(fw.load("uart_rx"), cycles or end, P=B, stimulus=stim)


@pytest.mark.parametrize("B", UART_B)
def test_uart_rx_random_bytes(B):
    rng = random.Random(1000 + B)
    data = [rng.randrange(256) for _ in range(8)]
    res = run_rx(uart_stimulus(RX, data, B, gap=rng.random()), B)
    assert res.host_rx[0] == data
    assert res.sync[0] == 0 and not res.ems[0].LATE and not res.ems[0].OVF


@pytest.mark.parametrize("B", [16, 434, 5208])
def test_uart_rx_back_to_back(B):
    data = [0x00, 0xFF, 0x55, 0xAA, 0x80, 0x01]
    assert run_rx(uart_stimulus(RX, data, B, gap=0), B).host_rx[0] == data


@pytest.mark.parametrize("err", [-0.03, -0.02, 0.02, 0.03])
def test_uart_rx_tolerates_peer_clock_error(err):
    B = 434
    data = [0x00, 0xFF, 0x55, 0xAA, 0x0F]
    res = run_rx(uart_stimulus(RX, data, B * (1 + err), gap=0), B)
    assert res.host_rx[0] == data


def test_uart_rx_framing_error_sets_flag_and_drops_byte():
    B = 434
    stim = uart_stimulus(RX, [0x41, 0x42, 0x43], B, gap=1, stop_bits=[1, 0, 1])
    res = run_rx(stim, B)
    assert res.host_rx[0] == [0x41, 0x43] and res.sync[0] == 1


def test_uart_rx_ignores_a_short_glitch():
    B = 434
    stim = [(100, RX, 0), (100 + B // 4, RX, 1)] + uart_stimulus(RX, [0x5A], B, start=2000)
    assert run_rx(stim, B).host_rx[0] == [0x5A]


def test_uart_rx_minimum_bit_period():
    """Measured: back-to-back frames are received down to B = 9. At B = 8 the program misses
    the next start edge and loses data without raising any flag."""
    data = [0x00, 0xFF, 0x55, 0xAA]
    assert run_rx(uart_stimulus(RX, data, 9, gap=0), 9).host_rx[0] == data
    res = run_rx(uart_stimulus(RX, data, 8, gap=0), 8)
    assert res.host_rx[0] != data and not res.ems[0].LATE


def test_uart_loopback_tx_to_rx_on_two_ems():
    """EM0 transmits on B0, EM1 receives; the peer wiring copies B0 onto M0."""
    B = 100
    data = [0x12, 0x34, 0xAB]

    from iss import Device

    class Loop(Device):
        def attach(self, sim):
            super().attach(sim)
            self.prev = sim.pad[0]

        def on_cycle(self, c):
            v = self.sim.pad[0]
            if v != self.prev:
                self.sim.set_external(RX, v, c + 1)
                self.prev = v

    sim = Sim([fw.load("uart_tx"), fw.load("uart_rx")], P=[B, B], devices=[Loop()],
              host_tx=[[(0, b) for b in data], []])
    res = sim.run(40 * B)
    assert res.host_rx[1] == data


# -- SPI master ---------------------------------------------------------------------------

SCK, MOSI, MISO, CS = 2, 3, 9, 5


@pytest.mark.parametrize("P", [5, 10, 25, 1000])
def test_spi_master_full_duplex(P):
    rng = random.Random(P)
    out = [rng.randrange(256) for _ in range(6)]
    back = [rng.randrange(256) for _ in range(6)]
    slave = SpiSlave(SCK, MOSI, MISO, CS, responses=list(back))
    res = Sim([fw.load("spi_master")], P=[P], devices=[slave],
              host_tx=[[(0, b) for b in out]]).run(200 * P + 1000)
    assert slave.received == out
    assert res.host_rx[0] == back
    assert slave.errors == [] and not res.ems[0].LATE
    # SCK period is exactly 2P within a byte
    r = slave.rise_times
    assert all(b - a == 2 * P for a, b in zip(r, r[1:]) if b - a < 4 * P)


def test_spi_master_minimum_half_period():
    """Measured: P = 5 (SCK = 5 MHz at 50 MHz) is the fastest on-time clock; P = 4 is late."""
    slave = SpiSlave(SCK, MOSI, MISO, CS)
    res = Sim([fw.load("spi_master")], P=[4], devices=[slave],
              host_tx=[[(0, 0x5A)]]).run(1000)
    assert res.ems[0].LATE


def test_spi_master_cs_high_time_between_bytes():
    P = 20
    slave = SpiSlave(SCK, MOSI, MISO, CS)
    res = Sim([fw.load("spi_master")], P=[P], devices=[slave],
              host_tx=[[(0, 1), (0, 2), (0, 3)]]).run(5000)
    cs = res.pin_trace(CS)
    highs = [b[0] - a[0] for a, b in zip(cs, cs[1:]) if a[1] == 1]
    assert highs and min(highs) >= P


# -- I2C master ---------------------------------------------------------------------------

SCL, SDA = 6, 7
I2C_CFG = {"OUT_MSB": 1, "OUT_N": 7}


def run_i2c(data, P, slave, cycles=None, arrivals=None):
    arrivals = arrivals or [0] * len(data)
    sim = Sim([fw.load("i2c_master")], P=[P], cfg=[I2C_CFG], devices=[slave],
              host_tx=[list(zip(arrivals, data))])
    return sim.run(cycles or (len(data) + 2) * 25 * P + 1000)


@pytest.mark.parametrize("P", [65, 250])      # ~385 kHz and 100 kHz at 50 MHz
def test_i2c_write_transaction(P):
    slave = I2cSlave(SCL, SDA, address=0x50)
    data = [0x50 << 1, 0x12, 0xA5, 0x00]
    res = run_i2c(data, P, slave)
    assert slave.transactions == [data]
    assert [e for _, e in slave.events] == ["START", "ACK", "ACK", "ACK", "ACK", "STOP"]
    assert slave.errors == [] and res.sync[1] == 0 and not res.ems[0].LATE
    t = i2c_timing(res.trace, SCL, SDA)
    assert t["low"] >= P and t["high"] >= P and t["start_hold"] >= P and t["stop_setup"] >= P
    # Against the I2C minimums at 50 MHz (20 ns/cycle): fast mode needs tLOW 1.3 us,
    # tHIGH 0.6 us; standard mode tLOW 4.7 us, tHIGH 4.0 us.
    lo, hi = (65, 30) if P < 250 else (235, 200)
    assert t["low"] >= lo and t["high"] >= hi


def test_i2c_nack_on_address_stops_and_flags():
    slave = I2cSlave(SCL, SDA, address=0x50)
    res = run_i2c([0x51 << 1], 60, slave)
    assert [e for _, e in slave.events] == ["START", "NACK", "STOP"]
    assert res.sync[1] == 1


def test_i2c_nack_on_data_byte():
    slave = I2cSlave(SCL, SDA, address=0x50, nack_at=1)
    res = run_i2c([0x50 << 1, 0x99], 60, slave)
    assert [e for _, e in slave.events] == ["START", "ACK", "NACK", "STOP"]
    assert res.sync[1] == 1


def test_i2c_clock_stretching():
    P = 60
    slave = I2cSlave(SCL, SDA, address=0x50, stretch=700)
    data = [0x50 << 1, 0x01, 0x02]
    res = run_i2c(data, P, slave, cycles=20_000)
    assert slave.transactions == [data] and slave.errors == []
    assert sum(1 for _, e in slave.events if e == "STRETCH") == 3
    t = i2c_timing(res.trace, SCL, SDA)
    assert t["high"] >= P                   # no SCL high pulse is cut short by the stretch


def test_i2c_two_transactions_keep_bus_free_time():
    P = 60
    slave = I2cSlave(SCL, SDA, address=0x50)
    # The second transaction's address byte arrives while the first is still running.
    data = [0x50 << 1, 0x10, 0x50 << 1, 0x20]
    res = run_i2c(data, P, slave, arrivals=[0, 0, 3000, 3000])
    assert slave.transactions == [[0xA0, 0x10], [0xA0, 0x20]]
    t = i2c_timing(res.trace, SCL, SDA)
    assert t["bus_free"] >= P
