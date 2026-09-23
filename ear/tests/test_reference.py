"""The Ear reference: input path, feature extractor and classifier."""

import random

import pytest

from ear import protosim as ps
from ear.features import (INFER_CYCLES, MAX_CYCLES, N_FEATURES, PAIRS, extract, log2_code)
from ear.inpath import (BYPASS, LATENCY, MAJ_2OF3, MAJ_3OF5, filter_cycles, filter_edges,
                        filter_pins)
from ear.mlp import Config, Model

# -- input path -----------------------------------------------------------------------------


def _random_levels(rng, n, init):
    u, lvl = [], init
    while len(u) < n:
        u += [lvl] * rng.choice([1, 1, 2, 3, 4, 5, 7, 9, 20])
        lvl ^= 1
    return u[:n]


@pytest.mark.parametrize("mode", [BYPASS, MAJ_2OF3, MAJ_3OF5])
def test_event_driven_filter_equals_cycle_reference(mode):
    rng = random.Random(mode)
    for _ in range(100):
        init, n = rng.randrange(2), 400
        u = _random_levels(rng, n, init)
        edges = [(k, u[k]) for k in range(n) if u[k] != (u[k - 1] if k else init)]
        filt, _ = filter_cycles(u, mode, before=init)
        ref = [(k, filt[k]) for k in range(n) if filt[k] != (filt[k - 1] if k else init)]
        assert [e for e in filter_edges(init, edges, mode) if e[0] < n] == ref


@pytest.mark.parametrize("mode", [BYPASS, MAJ_2OF3, MAJ_3OF5])
def test_filter_latency_and_glitch_rejection(mode):
    assert filter_edges(0, [(100, 1)], mode) == [(100 + LATENCY[mode], 1)]
    one = filter_edges(0, [(100, 1), (101, 0)], mode)
    two = filter_edges(0, [(100, 1), (102, 0)], mode)
    assert (one == []) == (mode != BYPASS)                  # 1-cycle glitch: only bypass passes it
    assert (two == []) == (mode == MAJ_3OF5)                # 2-cycle pulse: only 3-of-5 rejects it


# -- features -------------------------------------------------------------------------------

def test_log2_code():
    assert [log2_code(i) for i in (1, 2, 3, 4, 5, 6, 7, 8)] == [0, 4, 6, 8, 9, 10, 11, 12]
    assert log2_code(65535) == 63 and log2_code(10 ** 6) == 63
    for i in range(1, 5000):                                # monotone
        assert log2_code(i) <= log2_code(i + 1)


def _toggle(pin, times):
    lvl, out = 0, []
    for t in times:
        lvl ^= 1
        out.append((t, pin, lvl))
    return out


def test_histogram_shifts_when_a_shorter_interval_arrives():
    # intervals 8, 8, 4 on pin 0: 8 is 2x the new minimum 4 -> bin 2 (1x, 1.4x, 2x, ...)
    w = extract([0, 0, 0, 0], _toggle(0, [0, 8, 16, 20]), 70_000, max_edges=4)
    hist = w[0].features[0:8]
    assert hist == [1, 0, 2, 0, 0, 0, 0, 0]
    assert w[0].features[8] == log2_code(4) and w[0].features[9] == log2_code(8)


def test_histogram_is_invariant_to_power_of_two_speed_changes():
    cap = ps.uart(random.Random(1), duration=200_000, period=40.0, duplex=False)
    base = [(t, 0, v) for t, _, v in cap.edges]
    windows = {}
    for scale in (1, 2, 4):
        e = [(t * scale, p, v) for t, p, v in base]
        windows[scale] = extract([1, 0, 0, 0], e, 200_000 * scale, max_cycles=10 ** 9)[0].features
    assert windows[1][0:8] == windows[2][0:8] == windows[4][0:8]
    assert windows[2][8] == windows[1][8] + 4 and windows[4][8] == windows[1][8] + 8


def test_window_closes_at_256_edges_then_pauses():
    edges = _toggle(1, range(10, 10 + 10 * 600, 10))
    ws = extract([0, 0, 0, 0], edges, 20_000)
    first = ws[0]
    assert first.start == 0 and first.end == 10 + 10 * 255 and first.edges == 256
    assert ws[1].start == first.end + 1 + INFER_CYCLES
    assert ws[0].features[12 + 10] == 255                   # pin 1 edge count saturates
    assert ws[0].features[12 + 11] == 127                   # high 1,280 of 2,561 cycles: 256*1280//2561


def test_window_closes_by_time_when_the_bus_is_slow():
    edges = _toggle(2, range(500, 300_000, 1000))
    ws = extract([0, 0, 0, 0], edges, 300_000)
    assert ws[0].end == MAX_CYCLES - 1 and ws[0].edges == 66
    assert ws[1].start == MAX_CYCLES + INFER_CYCLES


def test_pair_features():
    # pin 0 is a clock (period 20), pin 1 changes 2 cycles after each rising clock edge and
    # only while the clock is high
    clk = _toggle(0, range(10, 1000, 10))
    data = [(t + 2, 1, (t // 20) % 2) for t, _, v in clk if v == 1]
    lvl, dedup = 0, []
    for t, p, v in data:
        if v != lvl:
            dedup.append((t, p, v))
            lvl = v
    ws = extract([0, 0, 0, 0], sorted(clk + dedup), 70_000, max_edges=10_000)
    f = ws[0].features
    near = dict(zip(PAIRS, f[48:60]))
    hi = dict(zip(PAIRS, f[60:72]))
    assert near[(0, 1)] == len(dedup) and hi[(0, 1)] == len(dedup)
    assert near[(1, 0)] == 0                                 # clock edges never follow data within 4
    assert near[(2, 1)] == 0 and hi[(2, 1)] == 0             # pin 2 is idle low


def test_features_are_bytes_and_complete():
    rng = random.Random(3)
    for label in ps.CLASSES:
        cap = ps.generate(label, rng)
        init, edges = ps.to_pins(cap, ps.random_assignment(cap, rng))
        ws = extract(init, filter_pins(init, edges, BYPASS), cap.duration)
        assert ws, label
        for w in ws:
            assert len(w.features) == N_FEATURES
            assert all(0 <= x <= 255 for x in w.features)


# -- classifier -------------------------------------------------------------------------

def _random_model(rng):
    w1 = [[rng.choice([-1, 0, 1]) for _ in range(72)] for _ in range(8)]
    w2 = [[rng.choice([-1, 0, 1]) for _ in range(8)] for _ in range(8)]
    return Model(w1, w2)


def test_weight_chain_round_trip():
    rng = random.Random(4)
    m = _random_model(rng)
    b = m.to_bytes()
    assert len(b) == 160
    assert Model.from_bytes(b).weights() == m.weights()
    assert Model.from_hex(m.to_hex()).weights() == m.weights()
    # weight 0 is in the two least significant bits of byte 0
    m.w1[0][0] = -1
    assert m.to_bytes()[0] & 3 == 0b11
    with pytest.raises(ValueError):
        Model.from_bytes(bytes([0b10]) + bytes(159))


def test_inference_by_hand():
    w1 = [[0] * 72 for _ in range(8)]
    w2 = [[0] * 8 for _ in range(8)]
    w1[0][0], w1[0][1] = 1, -1                  # h0 = relu(f0 - f1) >> shift
    w1[1][2] = 1                                # h1 = f2 >> shift
    w2[3][0], w2[5][1] = 1, 1
    m = Model(w1, w2, Config(shift=1, margin=5, floor=10, min_edges=24))
    f = [0] * 72
    f[0], f[1], f[2] = 200, 50, 40
    r = m.infer(f, edges=100)
    assert r.hidden[:2] == [75, 20] and r.logits[3] == 75 and r.logits[5] == 20
    assert (r.cls, r.confident, r.ood) == (3, True, False)
    assert m.infer(f, edges=10).ood                          # activity gate
    f[0] = 50
    r = m.infer(f, edges=100)
    assert r.cls == 5 and r.logits[3] == 0 and r.ood is False
    f[2] = 10
    assert m.infer(f, edges=100).ood                          # best logit 5 < floor 10


def test_accumulator_fits_16_bits_for_any_input():
    ones = Model([[1] * 72 for _ in range(8)], [[1] * 8 for _ in range(8)], Config(shift=0))
    r = ones.infer([255] * 72)
    assert r.hidden == [255] * 8 and r.logits == [2040] * 8
    assert 72 * 255 < 32768
