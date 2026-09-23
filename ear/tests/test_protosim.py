"""Every generator's edge stream decodes back to the payload it encoded."""

import random

import pytest

from ear import protosim as ps


def levels(cap):
    """level(line, t): the line's level at cycle t."""
    per = {i: [] for i in range(len(cap.lines))}
    for t, ln, v in cap.edges:
        per[ln].append((t, v))

    def lvl(line, t):
        v = cap.initial[line]
        for tt, vv in per[line]:
            if tt > t:
                break
            v = vv
        return v
    return lvl, per


def edges_of(per, line, level=None):
    return [t for t, v in per[line] if level is None or v == level]


@pytest.mark.parametrize("seed", range(6))
def test_uart_decodes(seed):
    rng = random.Random(seed)
    cap = ps.uart(rng, duration=200_000, duplex=False, period=rng.uniform(17, 1500))
    B, stop = cap.params["period"], cap.params["stop"]
    nbits = cap.params["data_bits"] + (1 if cap.params["parity"] else 0)
    lvl, per = levels(cap)
    got, t_free = [], -1
    for t in edges_of(per, 0, 0):
        if t < t_free or lvl(0, t - 1) != 1:
            continue
        byte = sum(lvl(0, int(t + (k + 1.5) * B)) << k for k in range(cap.params["data_bits"]))
        got.append(byte)
        t_free = t + (nbits + 1.5) * B
    assert got == cap.payload["TX"] and got


@pytest.mark.parametrize("mode", range(4))
def test_spi_decodes(mode):
    cap = ps.spi(random.Random(mode), duration=100_000, half=10, mode=mode)
    lvl, per = levels(cap)
    cpol, cpha = mode >> 1, mode & 1
    sample_level = 1 - cpol if cpha == 0 else cpol          # the sampling edge's new level
    samples = [t for t in edges_of(per, 0, sample_level) if lvl(3, t) == 0]
    bits = [lvl(1, t - 1) for t in samples]                  # MOSI just before the edge
    got = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    assert got == cap.payload["mosi"] and got


def test_i2c_decodes():
    cap = ps.i2c(random.Random(3), duration=300_000, half=62.5, stretch=True)
    lvl, per = levels(cap)
    events = sorted([(t, "SCL", v) for t, v in per[0]] + [(t, "SDA", v) for t, v in per[1]])
    txs, cur, bits = [], None, []
    for t, ln, v in events:
        if ln == "SDA" and lvl(0, t) == 1:
            if v == 0:
                cur, bits = [], []
            else:
                txs.append(cur)
                cur = None
        elif ln == "SCL" and v == 1 and cur is not None:
            bits.append(lvl(1, t))
            if len(bits) == 9:
                cur.append(int("".join(map(str, bits[:8])), 2))
                bits = []
    assert txs == cap.payload["transactions"] and txs


def test_usb_ls_decodes():
    cap = ps.usb_ls(random.Random(4), duration=150_000)
    lvl, per = levels(cap)
    Bt = cap.params["bit"]
    packets, t_free = [], -1
    for t in edges_of(per, 0, 1):                          # D+ rising = first K of SYNC
        if t < t_free or lvl(1, t - 1) != 1 or lvl(0, t - 1) != 0:
            continue
        state, raw, ones, k = 1, [], 0, 0
        while True:
            c = int(t + (k + 0.5) * Bt)
            dp, dm = lvl(0, c), lvl(1, c)
            if dp == 0 and dm == 0:
                break                                        # SE0: end of packet
            s = 1 if dm else 0
            bit = 1 if s == state else 0
            state = s
            k += 1
            if ones == 6:
                ones = 0                                     # stuffed bit, dropped
                continue
            raw.append(bit)
            ones = ones + 1 if bit else 0
        packets.append(raw)
        t_free = t + (k + 3) * Bt
    assert packets == cap.payload["packets"] and packets


def test_can_decodes():
    cap = ps.can(random.Random(5), duration=200_000, period=100.0)
    lvl, per = levels(cap)
    B = cap.params["period"]
    frames, t_free = [], -1
    for t in edges_of(per, 0, 0):
        if t < t_free or lvl(0, t - 1) != 1:
            continue
        raw, run, last, k = [], 0, None, 0
        while True:
            v = lvl(0, int(t + (k + 0.5) * B))
            k += 1
            if run == 5:
                run, last = 1, v                             # stuff bit
                continue
            raw.append(v)
            run = run + 1 if v == last else 1
            last = v
            if len(raw) >= 19:
                dlc = int("".join(map(str, raw[15:19])), 2)
                if len(raw) == 19 + 8 * dlc + 15:
                    break
        frames.append(raw)
        t_free = t + (k + 13) * B
    assert frames == cap.payload["frames"] and frames
    for f in frames:
        assert ps._can_crc(f[:-15]) == int("".join(map(str, f[-15:])), 2)


def test_jtag_and_swd_decode():
    cap = ps.jtag(random.Random(6), duration=60_000, half=7)
    lvl, per = levels(cap)
    assert [lvl(2, t - 1) for t in edges_of(per, 0, 1)] == cap.payload["tdi"]
    cap = ps.swd(random.Random(6), duration=60_000, half=7)
    lvl, per = levels(cap)
    sampled = [lvl(1, t - 1) for t in edges_of(per, 0, 1)]
    host = cap.payload["host_bits"]
    assert sampled[:54] == host[:54]                         # line reset + idle


def test_ps2_decodes():
    cap = ps.ps2(random.Random(7), duration=600_000)
    lvl, per = levels(cap)
    bits = [lvl(1, t) for t in edges_of(per, 0, 0)]
    frames = [bits[i:i + 11] for i in range(0, len(bits) - 10, 11)]
    got = [sum(b << k for k, b in enumerate(f[1:9])) for f in frames]
    assert got == cap.payload["data"] and got
    for f in frames:
        assert f[0] == 0 and f[10] == 1 and sum(f[1:10]) % 2 == 1


@pytest.mark.parametrize("pair", [False, True])
def test_manchester_decodes(pair):
    cap = ps.manchester(random.Random(8), duration=100_000, period=40.0, pair=pair)
    lvl, per = levels(cap)
    B = cap.params["period"]
    # Each burst starts with a 1 (low then high) after idle high: find the first fall after idle.
    starts, t_free = [], -1
    for t in edges_of(per, 0, 0):
        if t >= t_free and lvl(0, t - 1) == 1 and all(lvl(0, t - k) == 1 for k in range(1, 3)):
            starts.append(t)
            n = len(cap.payload["bursts"][len(starts) - 1])
            t_free = t + n * B + 2 * B
            if len(starts) == len(cap.payload["bursts"]):
                break
    for s, burst in zip(starts, cap.payload["bursts"]):
        got = [lvl(0, int(s + k * B + 0.75 * B)) for k in range(len(burst))]
        assert got == burst
        if pair:
            assert all(lvl(1, int(s + k * B + 0.25 * B)) != lvl(0, int(s + k * B + 0.25 * B))
                       for k in range(len(burst)))


@pytest.mark.parametrize("label", ps.CLASSES)
def test_every_class_generates_a_busy_stream(label):
    cap = ps.generate(label, random.Random(label))
    assert cap.label == label and len(cap.edges) > 100
    assert all(a[0] <= b[0] for a, b in zip(cap.edges, cap.edges[1:]))
    level = list(cap.initial)
    for t, ln, v in cap.edges:
        assert v != level[ln]                                # every edge changes the level
        level[ln] = v


def test_perturb_keeps_order_and_adds_glitches():
    cap = ps.uart(random.Random(9), duration=200_000, period=100.0, duplex=False)
    p = ps.perturb(cap, random.Random(9), jitter=3, glitches=2.0)
    assert len(p.edges) > len(cap.edges)
    level = list(p.initial)
    for t, ln, v in p.edges:
        assert v != level[ln]
        level[ln] = v
    assert p.min_pulse() <= 2


def test_pin_assignment():
    cap = ps.spi(random.Random(1), duration=10_000, half=5, mode=0)
    init, edges = ps.to_pins(cap, [3, None, 0, 1])
    assert init == [1, 1, 0, 1]
    assert {p for _, p, _ in edges} <= {0, 2, 3}
