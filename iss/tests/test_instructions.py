"""Semantics of every instruction, against the timing model in isa/isa.yaml.

Input latency L = 2 throughout: an external edge at cycle e is seen by the EM at e + 2.
"""

import pytest

from iss import Sim, simulate
from iss.isa import load_isa

from .progs import (ADDT, ADDTP, HALT, IN, JMP, OUT, PULL, PUSH, SETPIN, SETR, SHIFT, SYNC,
                    WAIT)

M = (1 << 24) - 1
LOW = {p: 0 for p in range(12)}          # all external levels low


def run(prog, cycles=10_000, **kw):
    kw.setdefault("log", True)
    return simulate(prog, cycles, **kw)


def done_at(res, pc):
    """Completion cycle of the first retirement of the instruction at pc."""
    return next(c for (c, _em, p, _n) in res.log if p == pc)


# -- HALT and illegal encodings -----------------------------------------------------------

def test_blank_memory_halts_at_cycle_0():
    res = run([])
    em = res.ems[0]
    assert em.halted and em.irq and em.PC == 0 and em.retired == 0


def test_halt_stops_execution():
    res = run([SETR("X", 5), HALT(), SETR("X", 9)])
    assert res.ems[0].halted and res.ems[0].X == 5 and res.ems[0].PC == 1


@pytest.mark.parametrize("word", [0xC000, 0xD000, 0xE000, 0xF123])
def test_undefined_opcodes_halt(word):
    res = run([word])
    assert res.ems[0].halted and "undefined opcode" in res.ems[0].halt_reason


E = load_isa().encode


@pytest.mark.parametrize("word", [
    E("OUT", pin=8, value=0, d=0),          # input-only pin
    E("OUT", pin=12, value=0, d=0),
    E("SET", target="pin", pin=9, level=1, od=0),
    E("WAIT", pin=12, mode=0, timeout=0),
    E("WAIT", pin=0, mode=5, timeout=0),
    E("WAIT", pin=0, mode=0, timeout=24),
    E("IN", pin=13, snap=0, d=0),
    E("JMP", cond=5, pin=0, target=0),
    E("JMP", cond=0, pin=5, target=0),
    E("JMP", cond=3, pin=12, target=0),
])
def test_illegal_operands_halt(word):
    res = run([word])
    assert res.ems[0].halted and res.ems[0].retired == 0


# -- WAIT ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,edges,expect_T", [
    ("rise", [(100, 8, 1)], 102),
    ("fall", [(100, 8, 1), (150, 8, 0)], 152),
    ("any", [(100, 8, 1)], 102),
    ("high", [(100, 8, 1)], 102),
])
def test_wait_modes(mode, edges, expect_T):
    res = run([WAIT(8, mode), HALT()], stimulus=edges, initial=LOW)
    em = res.ems[0]
    assert em.T == expect_T and done_at(res, 0) == expect_T and em.PC == 1


def test_wait_level_matches_immediately():
    res = run([WAIT(3, "low"), HALT()], initial=LOW)
    assert res.ems[0].T == 0 and done_at(res, 0) == 0


def test_wait_ignores_edges_before_it_starts():
    # Edge visible at cycle 2; WAIT starts at cycle 3, so it needs the next rising edge.
    prog = [ADDT(0), ADDT(0), ADDT(0), WAIT(8, "rise"), HALT()]
    stim = [(0, 8, 1), (40, 8, 0), (80, 8, 1)]
    res = run(prog, stimulus=stim, initial=LOW)
    assert res.ems[0].T == 82


def test_wait_latches_snapshot_at_the_edge():
    stim = [(10, 9, 1), (100, 8, 1), (101, 9, 0)]
    res = run([WAIT(8, "rise"), HALT()], stimulus=stim, initial=LOW)
    snap = res.ems[0].SNAPSHOT
    assert (snap >> 8) & 1 == 1 and (snap >> 9) & 1 == 1 and (snap >> 0) & 1 == 0


def test_wait_timeout_sets_TO_and_moves_T_to_deadline():
    # T = 0 at reset; timeout 2^4 = 16 cycles after T.
    prog = [WAIT(8, "rise", 4), JMP(3, "flag", "TO"), HALT(), SETR("X", 5), HALT()]
    res = run(prog, initial=LOW)
    em = res.ems[0]
    assert em.T == 16 and done_at(res, 0) == 16
    assert em.X == 5 and em.TO is False            # JMP TO jumped and cleared TO


def test_wait_match_at_deadline_beats_timeout():
    res = run([WAIT(8, "rise", 4), HALT()], stimulus=[(14, 8, 1)], initial=LOW)
    assert res.ems[0].T == 16 and res.ems[0].TO is False


def test_wait_timeout_already_expired_times_out_at_once():
    prog = [ADDT(0), ADDT(0), ADDT(0), ADDT(0), WAIT(8, "rise", 1), HALT()]
    res = run(prog, initial=LOW)
    em = res.ems[0]
    assert em.TO and done_at(res, 4) == 4 and em.T == 2


# -- OUT and the scheduler ----------------------------------------------------------------

def test_out_drives_at_exactly_T_plus_d():
    res = run([ADDT(100), OUT(0, 0, 5), HALT()])
    assert res.pin_trace(0) == [(105, 0)]
    assert not res.ems[0].LATE


def test_out_with_d_max():
    res = run([ADDT(100), OUT(0, 0, 63), HALT()])
    assert res.pin_trace(0) == [(163, 0)]


def _osr_program(order, n, byte_bits_out):
    prog = [SHIFT("out", order, n), PULL(), ADDT(100)]
    for _ in range(byte_bits_out):
        prog += [OUT(0, "OSR", 0), ADDT(10)]
    return prog + [HALT()]


@pytest.mark.parametrize("order,n,byte", [("lsb", 8, 0b10110010), ("msb", 8, 0b10110010),
                                          ("msb", 5, 0b00010110), ("lsb", 3, 0b101)])
def test_out_osr_consumes_one_bit_per_execution(order, n, byte):
    res = run(_osr_program(order, n, n), host_tx=[(0, byte)])
    bits = [(byte >> i) & 1 for i in range(n)]
    if order == "msb":
        bits = bits[::-1]
    got = [res.level_at(0, 100 + 10 * k) for k in range(n)]
    assert got == bits
    em = res.ems[0]
    assert em.OSR_COUNT == n and not em.UNF


def test_out_not_osr_peeks_without_consuming():
    prog = [SHIFT("out", "lsb", 8), PULL(), ADDT(100), OUT(0, "~OSR", 0), ADDT(10),
            OUT(0, "OSR", 0), HALT()]
    res = run(prog, host_tx=[(0, 0b1)], initial=LOW)
    assert res.level_at(0, 100) == 0 and res.level_at(0, 110) == 1
    assert res.ems[0].OSR_COUNT == 1


def test_out_osr_empty_without_autopull_sets_UNF():
    res = run([ADDT(50), OUT(0, "OSR", 0), HALT()])
    em = res.ems[0]
    assert em.UNF and res.level_at(0, 50) == 0 and em.OSR_COUNT == 8


def test_out_osr_autopull():
    prog = [SHIFT("out", "lsb", 8, auto=1), ADDT(50), OUT(0, "OSR", 0), ADDT(10),
            OUT(0, "OSR", 0), HALT()]
    res = run(prog, host_tx=[(0, 0b10)])
    assert res.level_at(0, 50) == 0 and res.level_at(0, 60) == 1
    assert not res.ems[0].UNF and res.ems[0].tx == []


def test_out_autopull_blocks_until_data():
    prog = [SHIFT("out", "lsb", 8, auto=1), ADDT(500), OUT(0, "OSR", 0), HALT()]
    res = run(prog, host_tx=[(300, 0)])
    assert done_at(res, 2) == 300 and res.pin_trace(0) == [(500, 0)]


def test_two_slot_blocking_rule():
    # Two future OUTs fill both slots; the third blocks until the first slot fires at 100,
    # arms in that same cycle, and the next instruction runs at 101.
    prog = [ADDT(100), OUT(0, 0), ADDT(10), OUT(0, 1), ADDT(10), OUT(0, 0), HALT()]
    res = run(prog)
    assert done_at(res, 1) == 1 and done_at(res, 3) == 3
    assert done_at(res, 5) == 100 and done_at(res, 6) == 101
    assert res.pin_trace(0) == [(100, 0), (110, 1), (120, 0)]
    assert not res.ems[0].LATE


def test_same_pin_same_time_later_armed_wins():
    res = run([ADDT(50), OUT(0, 1), OUT(0, 0), HALT()], initial={0: 0})
    assert res.pin_trace(0) == []              # the later OUT (0) wins; pad stays low
    res = run([ADDT(50), OUT(0, 0), OUT(0, 1), HALT()], initial={0: 0})
    assert res.pin_trace(0) == [(50, 1)]


def test_addt_now_then_out_at_T_plus_0_is_late():
    """The v1.0 spec example: T := counter, then OUT @T+0 one cycle later is already late."""
    prog = [ADDT(0, now=1), OUT(0, 0, 0), JMP(4, "flag", "LATE"), HALT(), SETR("Y", 1), HALT()]
    res = run(prog)
    em = res.ems[0]
    assert em.LATE and res.pin_trace(0) == [(2, 0)]     # takes effect at c+1
    assert em.Y == 1                                     # JMP LATE taken; LATE stays set
    assert em.LATE


def test_addt_2_now_then_out_is_on_time():
    res = run([ADDT(2, now=1), OUT(0, 0, 0), HALT()])
    assert not res.ems[0].LATE and res.pin_trace(0) == [(2, 0)]


# -- IN -----------------------------------------------------------------------------------

def test_in_samples_at_T_plus_d():
    # Pin 9 rises at 48, visible at 50. IN @T+0 with T=49 samples 0 at 49;
    # IN @T+1 executes at 50 with target 50: on time, samples 1.
    prog = [SHIFT("in", "lsb", 2), ADDT(49), IN(9, 0), IN(9, 1), PUSH(), HALT()]
    res = run(prog, stimulus=[(48, 9, 1)], initial=LOW)
    assert done_at(res, 2) == 49 and done_at(res, 3) == 50
    assert res.host_rx[0] == [0b10] and not res.ems[0].LATE


def test_in_msb_order():
    prog = [SHIFT("in", "msb", 3), IN(9, 0), IN(8, 0), IN(8, 0), PUSH(), HALT()]
    res = run(prog, initial={**LOW, 9: 1})
    assert res.host_rx[0] == [0b100]


def test_in_snapshot_reads_the_level_at_the_edge():
    prog = [SHIFT("in", "lsb", 1, auto=1), WAIT(8, "rise"), IN(9, snap=1), HALT()]
    stim = [(5, 9, 1), (100, 8, 1), (101, 9, 0)]
    res = run(prog, stimulus=stim, initial=LOW)
    assert res.host_rx[0] == [1] and done_at(res, 2) == 103


def test_in_autopush_two_byte_word():
    prog = [SHIFT("in", "lsb", 9, auto=1)] + [IN(9, 0)] * 9 + [HALT()]
    res = run(prog, initial={**LOW, 9: 1})
    assert res.host_rx[0] == [0xFF, 0x01]


def test_in_late_when_target_passed():
    prog = [ADDT(0, now=1), IN(9, 0), HALT()]
    res = run(prog)
    assert res.ems[0].LATE and done_at(res, 1) == 1


def test_in_full_isr_drops_bits_without_autopush():
    prog = [SHIFT("in", "lsb", 2), IN(9, 0), IN(9, 0), IN(8, 0), PUSH(), HALT()]
    res = run(prog, initial={**LOW, 9: 1, 8: 0})
    assert res.host_rx[0] == [0b11]


# -- ADDT and ADDT P ----------------------------------------------------------------------

def test_addt_adds():
    res = run([ADDT(2047), ADDT(1), HALT()])
    assert res.ems[0].T == 2048


def test_addt_now_keeps_a_later_T():
    res = run([ADDT(1000), ADDT(2, now=1), HALT()])
    assert res.ems[0].T == 1000


def test_addt_now_uses_counter_when_T_passed():
    res = run([ADDT(5, now=1), HALT()])
    assert res.ems[0].T == 5


def test_addt_now_after_idle_longer_than_half_the_counter():
    # T = 5 is written in the future; the EM then idles 9M cycles in PULL (> 2^23) so a
    # plain modular compare would misread T = 5 as "later". T_PASSED makes it use the counter.
    prog = [ADDT(5), PULL(), ADDT(2, now=1), HALT()]
    res = run(prog, 20_000_000, host_tx=[(9_000_000, 0x55)])
    assert res.ems[0].T == (9_000_001 + 2) & M


def test_counter_wrap():
    start = (1 << 24) - 3                   # counter wraps to 0 at cycle 3
    prog = [ADDT(10), ADDT(2, now=1), OUT(0, 0, 0), HALT()]
    res = run(prog, counter_offset=start)
    em = res.ems[0]
    assert em.T == 7 and not em.LATE
    assert res.pin_trace(0) == [(10, 0)]


@pytest.mark.parametrize("shift,delta", [(0, 1003), (1, 501), (2, 250), (3, 125)])
def test_addt_p(shift, delta):
    res = run([ADDT(100), ADDTP(shift), HALT()], P=1003)
    assert res.ems[0].T == 100 + delta


def test_addt_p_now():
    res = run([ADDTP(0, now=1), HALT()], P=40000)
    assert res.ems[0].T == 40000


# -- SHIFT --------------------------------------------------------------------------------

def test_shift_writes_config_only():
    res = run([SHIFT("out", "msb", 16, auto=1), SHIFT("in", "msb", 3, auto=1), HALT()])
    em = res.ems[0]
    assert (em.OUT_MSB, em.OUT_N, em.AUTOPULL) == (1, 15, 1)
    assert (em.IN_MSB, em.IN_N, em.AUTOPUSH) == (1, 2, 1)
    assert em.osr_empty and em.ISR_COUNT == 0


# -- PUSH / PULL --------------------------------------------------------------------------

def test_pull_blocks_until_host_writes():
    res = run([PULL(), HALT()], host_tx=[(250, 0xA5)])
    assert done_at(res, 0) == 250 and res.ems[0].OSR == 0xA5 and res.ems[0].OSR_COUNT == 0


def test_pull_nonblocking_empty_leaves_osr():
    res = run([PULL(0), HALT()])
    assert done_at(res, 0) == 0 and res.ems[0].osr_empty


def test_pull_two_byte_word_low_byte_first():
    res = run([SHIFT("out", "lsb", 16), PULL(), HALT()], host_tx=[(0, 0x34), (0, 0x12)])
    assert res.ems[0].OSR == 0x1234


def test_push_nonblocking_full_sets_ovf():
    prog = [PUSH(0)] * 5 + [HALT()]
    res = run(prog, rx_drain=False)
    assert res.rx_fifo[0] == [0, 0, 0, 0] and res.ems[0].OVF


def test_push_blocking_waits_for_room():
    prog = [PUSH(1)] * 5 + [HALT()]
    res = run(prog, rx_drain=False)
    assert not res.ems[0].halted and res.ems[0].PC == 4 and not res.ems[0].OVF


def test_tx_fifo_is_four_deep():
    res = run([HALT()], host_tx=[(0, b) for b in range(6)])
    assert res.tx_fifo[0] == [0, 1, 2, 3]


# -- SET ----------------------------------------------------------------------------------

def test_set_pin_push_pull_takes_effect_next_cycle():
    res = run([SETPIN(0, 1), SETPIN(0, 0), HALT()], initial={0: 0})
    assert res.pin_trace(0) == [(1, 1), (2, 0)]


def test_set_pin_open_drain_releases_to_external_level():
    stim = [(20, 1, 0), (40, 1, 1)]
    prog = [SETPIN(1, 0, od=1), ADDT(0), SETPIN(1, 1, od=1), HALT()]
    res = run(prog, stimulus=stim)
    # driven low at 1, released at 3 (external still 1 -> pad 1), then external 0 at 20, 1 at 40
    assert res.pin_trace(1) == [(1, 0), (3, 1), (20, 0), (40, 1)]


def test_open_drain_out_value_1_releases():
    prog = [SETPIN(2, 0, od=1), ADDT(30), OUT(2, 1), HALT()]
    res = run(prog, stimulus=[(10, 2, 0)])
    assert res.pin_trace(2) == [(1, 0)]          # released at 30, but the peer holds it low


def test_set_registers():
    res = run([SETR("X", 1023), SETR("Y", 7), SETR("P", 512), HALT()])
    em = res.ems[0]
    assert (em.X, em.Y, em.P) == (1023, 7, 512)


# -- JMP ----------------------------------------------------------------------------------

def test_jmp_x_decrement_runs_x_plus_one_passes():
    res = run([SETR("X", 3), JMP(1, "X--"), HALT()])
    assert res.ems[0].X == 0 and res.ems[0].retired == 5 and done_at(res, 2) == 5


def test_jmp_y_decrement():
    res = run([SETR("Y", 2), JMP(1, "Y--"), HALT()])
    assert res.ems[0].Y == 0 and res.ems[0].retired == 4


@pytest.mark.parametrize("cond,level,taken", [("high", 1, True), ("high", 0, False),
                                              ("low", 0, True), ("low", 1, False)])
def test_jmp_pin(cond, level, taken):
    prog = [JMP(2, cond, 11), HALT(), SETR("X", 1), HALT()]
    res = run(prog, initial={11: level})
    assert (res.ems[0].X == 1) == taken


def test_jmp_txe_and_osre():
    prog = [JMP(2, "flag", "TXE"), HALT(), SETR("X", 1), PULL(), JMP(6, "flag", "!OSRE"),
            HALT(), SETR("Y", 1), HALT()]
    res = run(prog, host_tx=[(10, 0)])
    assert res.ems[0].X == 1 and res.ems[0].Y == 1


def test_jmp_always():
    res = run([JMP(2), SETR("X", 1), HALT()])
    assert res.ems[0].X == 0


# -- SYNC ---------------------------------------------------------------------------------

def test_sync_wait_blocks_until_host_sets_then_clears():
    res = run([SYNC("wait", 2), HALT()], host_sync=[(70, 2, 1)])
    assert done_at(res, 0) == 70 and res.sync[2] == 0


def test_sync_between_two_ems():
    em0 = [ADDT(0)] * 5 + [SYNC("set", 1), HALT()]
    em1 = [SYNC("wait", 1), HALT()]
    sim = Sim([em0, em1], log=True)
    res = sim.run(1000)
    wait_done = next(c for (c, em, pc, _n) in res.log if em == 1 and pc == 0)
    assert wait_done == 5 and res.sync[1] == 0


def test_em1_wins_pin_conflict():
    em0 = [ADDT(20), OUT(0, 0), HALT()]
    em1 = [ADDT(20), OUT(0, 1), HALT()]
    res = Sim([em0, em1], initial={0: 0}).run(100)
    assert res.pin_trace(0) == [(20, 1)]


def test_host_tx_drop_mode_loses_bytes_written_to_a_full_fifo():
    # The chip's SPI port drops a byte written to a full TX FIFO; host_tx_drop models that.
    # The EM pulls one byte at cycle 3. Host writes land before the EM runs, so a write at
    # cycle 3 still finds the FIFO full and is lost; one at cycle 4 fits.
    from asm import assemble
    prog = assemble("SET X, 1\nw: JMP X--, w\nPULL\nHALT")
    tx = [(0, b) for b in (1, 2, 3, 4)]
    kept = Sim([prog], host_tx=[tx + [(1, 5), (3, 6), (4, 7)]], host_tx_drop=True,
               rx_drain=False).run(20)
    assert kept.ems[0].OSR == 1 and kept.ems[0].tx == [2, 3, 4, 7]
    waited = Sim([prog], host_tx=[tx + [(1, 5)]], rx_drain=False).run(20)
    assert waited.ems[0].tx == [2, 3, 4, 5]


def test_host_rx_pop_reads_one_byte_at_a_time():
    # PUSH at cycle 2 queues 1 and PUSH at cycle 5 queues 3. Host pops land before the EM
    # runs: the one at 3 takes the 1, the one at 5 finds the FIFO empty, the one at 6 takes 3.
    from asm import assemble
    prog = assemble("WAIT M0, high\nIN M0 @snap\nPUSH\nIN M0 @snap\nIN M0 @snap\nPUSH\nHALT")
    res = Sim([prog], rx_drain=False, host_rx_pop=[(3, 0), (5, 0), (6, 0)]).run(20)
    assert res.host_rx[0] == [1, 3] and res.ems[0].rx == []
