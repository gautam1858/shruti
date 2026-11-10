"""The Watch reference against hand-worked cases from watch/SPEC.md."""

from ear.features import log2_code
from watch.model import (CTRL, DATA, EDGE_ANY, EDGE_FALL, EDGE_RISE, HITS, MON, PINSEL, POST,
                         SRC, SRC_CLASS, SRC_EXT, SRC_MON0, SRC_MON1, SRC_OOD, Watch)


class Bench:
    """Drive a Watch one cycle at a time with pin levels; idle cycles keep the last level."""

    def __init__(self, w=None, level=0):
        self.w, self.level, self.t, self.outs = w or Watch(), level, 100, []

    def cycle(self, level=None, **kw):
        prev = self.level
        if level is not None:
            self.level = level
        o = self.w.step(self.level, prev, self.t, **kw)
        self.outs.append(o)
        self.t += 1
        return o

    def idle(self, n, **kw):
        for _ in range(n):
            self.cycle(**kw)

    def write(self, addr, data):
        return self.cycle(write=(addr, data))

    def toggle(self, pin):
        return self.cycle(self.level ^ (1 << pin))


def armed(**regs):
    b = Bench()
    for addr, d in regs.get("writes", []):
        b.write(addr, d)
    b.write(CTRL, 1)
    return b


def test_recorder_keeps_the_last_16_edges_oldest_first():
    b = armed()
    times = []
    for _ in range(20):
        times.append(b.t)
        b.toggle(0)
        b.idle(3)
    w = b.w
    assert w.count == 16
    ents = w.entries()
    assert [t for _, t in ents] == [t & 0xFFFF for t in times[4:]]
    assert [lv & 1 for lv, _ in ents] == [(k + 1) & 1 for k in range(4, 20)]


def test_edges_in_one_cycle_share_an_entry_and_pins_filters():
    b = armed(writes=[(PINSEL, 0xFE), (PINSEL + 1, 0x0F)])     # B0 not recorded
    b.cycle(0b110)                                             # B1 and B2 together
    b.idle(2)
    b.toggle(0)                                                # B0 only: not recorded
    b.idle(2)
    b.cycle(b.level | 1 << 11)                                 # M3
    ents = b.w.entries()
    assert [lv for lv, _ in ents] == [0b110, 0b1000_0000_0111]
    assert b.w.count == 2


def test_monitor_edge_direction_guard_and_hits():
    w = Watch()
    b = Bench(w)
    b.write(MON, 3 | EDGE_RISE << 4)                           # rise on B3
    b.write(MON + 2, 1 << 5)                                   # guard: B5 ...
    b.write(MON + 4, 1 << 5)                                   # ... high
    assert not b.toggle(3).match[0]                            # rise, B5 low
    b.toggle(3)                                                # fall
    b.cycle(b.level | 1 << 5)
    assert b.toggle(3).match[0]                                # rise with B5 high
    assert not b.toggle(3).match[0]                            # fall
    assert w.mons[0].hits == 1
    b.write(HITS, 0)
    assert w.mons[0].hits == 0
    b.write(MON, 3 | EDGE_FALL << 4)
    b.toggle(3)
    assert b.toggle(3).match[0]
    b.write(MON, 3 | EDGE_ANY << 4)
    assert b.toggle(3).match[0] and b.toggle(3).match[0]


def test_monitor_interval_bounds_catch_a_short_pulse():
    # Monitor 1 on B0, any edge, interval code at most log2_code(9): pulses up to 9 cycles.
    b = Bench()
    b.write(MON + 8, 0 | EDGE_ANY << 4)
    b.write(MON + 15, log2_code(9))
    assert not b.toggle(0).match[1]                            # first edge: interval 65,535
    b.idle(29)
    assert not b.toggle(0).match[1]                            # 30 cycles since
    b.idle(4)
    assert b.toggle(0).match[1]                                # 5 cycles: a glitch
    b.idle(8)
    assert b.toggle(0).match[1]                                # 9 cycles, the bound
    b.idle(9)
    assert not b.toggle(0).match[1]                            # 10 cycles
    b.write(MON + 8, 0 | EDGE_ANY << 4)                        # CFG write restarts it
    assert not b.toggle(0).match[1]


def test_monitor_trigger_keeps_post_entries_then_freezes():
    b = armed(writes=[(MON, 2 | EDGE_RISE << 4 | 1 << 6), (SRC, SRC_MON0), (POST, 3)])
    for _ in range(5):
        b.toggle(0)
        b.idle(2)
    t_trig = b.t
    o = b.toggle(2)                                            # the trigger edge
    assert o.fire and o.record
    w = b.w
    assert w.triggered and not w.frozen and w.trig_out and w.cause == SRC_MON0
    assert w.ttime == t_trig
    for k in range(3):
        b.idle(2)
        b.toggle(0)
    assert w.frozen and w.irq
    b.idle(2)
    b.toggle(0)                                                # after the freeze
    ents = w.entries()
    assert len(ents) == 9 and ents[5][1] == t_trig & 0xFFFF
    b.write(CTRL, 0)                                           # disarm: entries stay
    assert not w.frozen and not w.triggered and not w.armed and w.count == 9
    b.write(CTRL, 1)                                           # re-arm: cleared
    assert w.count == 0 and w.armed


def test_post_zero_freezes_with_the_trigger_entry():
    b = armed(writes=[(MON, 1 | EDGE_ANY << 4 | 1 << 6), (SRC, SRC_MON0), (POST, 0)])
    b.toggle(0)
    b.toggle(1)
    assert b.w.frozen
    b.toggle(0)
    assert [lv for lv, _ in b.w.entries()] == [1, 3]


def test_trigger_sources_ood_class_ext_and_host():
    for src, kw_on, kw_off in ((SRC_OOD, {"ear_ood": 1}, {"ear_ood": 0}),
                               (SRC_CLASS, {"ear_shown": 5}, {"ear_shown": 0}),
                               (SRC_EXT, {"trig_pad": 1}, {"trig_pad": 0})):
        b = armed(writes=[(SRC, src)])
        b.idle(3, **kw_off)
        fired = [b.cycle(**kw_on).fire for _ in range(4)]
        # OOD and class are registered Ear outputs: the change is seen at once. Trigger-in
        # goes through two synchroniser flops and an edge flop.
        assert fired == ([True, False, False, False] if src != SRC_EXT else
                         [False, False, True, False])
        assert b.w.cause == src
    # Not enabled: no trigger.
    b = armed(writes=[(SRC, SRC_MON1)])
    b.cycle(ear_ood=1)
    assert not b.w.triggered
    # The host.
    b.write(CTRL, 1)
    o = b.write(CTRL, 2)
    assert o.fire and b.w.triggered and b.w.armed and b.w.cause == 0
    b.write(CTRL, 2)                                           # already triggered
    assert b.w.triggered
    # Not armed: nothing.
    b = Bench()
    assert not b.write(CTRL, 2).fire


def test_data_port_reads_entries_and_restarts():
    b = armed()
    b.toggle(0)
    b.idle(1)
    b.cycle(b.level | 0b1001 << 8)                             # M0 and M3
    w = b.w
    (l0, t0), (l1, t1) = w.entries()
    got = []
    for _ in range(9):
        got.append(w.read(DATA))
        b.cycle(take=True)
    assert got == [t0 & 0xFF, t0 >> 8, l0 & 0xFF, l0 >> 8,
                   t1 & 0xFF, t1 >> 8, l1 & 0xFF, l1 >> 8, 0]
    b.write(DATA, 0)
    assert w.read(DATA) == t0 & 0xFF


def test_sync_flag_from_a_monitor():
    b = Bench()
    b.write(MON + 8, 4 | EDGE_RISE << 4 | 1 << 7)
    b.write(MON + 9, 2)
    assert b.toggle(4).sync_set == 1 << 2
    assert b.toggle(4).sync_set == 0


def test_hits_saturate_at_255():
    b = Bench()
    b.write(MON, 0 | EDGE_ANY << 4)
    for _ in range(300):
        b.toggle(0)
    assert b.w.mons[0].hits == 255
