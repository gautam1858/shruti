# The Ear: bit-exact definition

This is the definition the RTL must match. `ear/inpath.py`, `ear/features.py` and `ear/mlp.py` are its executable form. The architecture spec's "The Ear" section gives the intent. Where it was ambiguous or inconsistent, the choices below settle it; they are marked **(resolved)**.

## Input path

The Ear sees the same filtered pin levels as the Event Machines: the two-flop synchroniser, then the glitch filter (bypass, 2-of-3 or 3-of-5 majority), exactly as in `src/project.v`. A clean pin edge at cycle e reaches the filtered level at e + 2 (bypass), e + 4 (2-of-3) or e + 5 (3-of-5).

- 3-of-5 rejects pulses of 2 cycles or less, 2-of-3 pulses of 1 cycle.
- `ear/inpath.py` models this register by register.
- A cocotb test (`test/test.py`, `input_path_matches_reference_model`) checks the model against the RTL cycle by cycle, for all three modes and random pulses on all eight bus pins.

## Windows

- The host selects 4 of the 12 bus pins; "edges" below means filtered edges on those 4.
- A window closes at the end of the cycle in which its edge count reaches 256, or after 65,536 cycles, whichever comes first.
- The features are then fixed and the classifier runs for 648 cycles (below). During those cycles the counters are paused and edges are not counted; pin levels are still tracked.
- The next window starts on the following cycle, with every counter cleared and no previous edge remembered.

## Features (72 x 6 bits)

**Intervals.** An interval is the time between two consecutive edges on the same pin within a window, saturated at 65,535 cycles.

**Codes.** Its 6-bit code is `4 * m + ((I << 2) >> m & 3)`, where `m` is the leading-one position of I (0..15): the octave plus two mantissa bits. The half-octave index is `code >> 1`.

**Per pin (12 features, in canonical pin order, see below):**

| # | Feature | Width | Definition |
| --- | --- | --- | --- |
| 0-7 | hist[k] | 6-bit saturating | intervals whose half-octave index is k above the window's shortest so far (k = 7 collects 11x and longer) |
| 8 | min_code | 6 | code of the shortest interval; 63 if the pin had none |
| 9 | max_code | 6 | code of the longest "in-burst" interval, one within 6 half-octaves (less than 11.3x) of the shortest; 0 if none |
| 10 | edges | 6 | edges on this pin (an 8-bit saturating counter), top 6 bits |
| 11 | high | 6 | floor(64 x cycles high / window length), at most 63 |

**(resolved) Histogram against a moving minimum.** The spec bins each interval by its ratio to "the shortest interval seen in the window", but that is not known until the window ends, and there is no room to store intervals. The hardware therefore bins against the running minimum. When a new interval is shorter by s half-octaves, every bin k moves to bin min(7, k + s), with saturating merges into bin 7, before the new interval is counted in bin 0. For power-of-two speed changes this gives exactly the same histogram (tested at 1x, 2x and 4x).

**(resolved) "Longest in-burst interval".** This was undefined; it is now "within 6 half-octaves of the running minimum". If the minimum drops so far that the recorded longest falls outside that range, the longest restarts from the new interval.

**Per ordered pair (a, b)**, in the order (0,1) (0,2) (0,3) (1,0) (1,2) (1,3) (2,0) (2,1) (2,3) (3,0) (3,1) (3,2):
- **Features 48-59, near[a][b]** (6-bit saturating): b edges no more than 4 cycles after the latest a edge in the window, including an a edge in the same cycle.
- **Features 60-71, hi[a][b]** (6-bit saturating): b edges while a's filtered level is high, including a change of a in that same cycle.

**(resolved) Time-high fraction.** The spec gives no divider. The fraction needs one 16-by-16 division per pin per window; a serial restoring divider (6 quotient bits, 6 cycles per pin) fits inside the 648-cycle inference.

**(resolved) Every feature is 6 bits.** The v1.0 spec had 8-bit edge counts and time-high fractions next to 6-bit counters. With ternary weights a feature cannot be scaled down, so the two 8-bit features dominated the dot products. Dropping their 2 low bits raised the integer model's test accuracy from about 80% to about 88% on simulated data (ear/REPORT.md has the current numbers), and it costs nothing.

**(new) Canonical pin order.** At the end of each window the four pins are sorted by (edge count, time-high fraction), largest first, ties by pin number. Features are emitted in that order, and the pair features are remapped the same way. The features then do not depend on which physical pins the user wired the bus to. On simulated data this took an unconstrained float network from about 62% to about 92% accuracy. In hardware it is a 4-input sorting network on 12-bit keys, 5 compare-and-swap stages, plus a remap of the serial feature read address: roughly 150 cells. The counters stay per pin.

## Classifier

The classifier is a ternary MLP with 72 inputs, 8 hidden units and 8 outputs, and no biases:

```
acc_j = B[j] + sum_i W1[j][i] * f[i]       16-bit signed (|acc| <= 128 + 72 * 63 = 4,664)
h_j   = min(255, max(0, acc_j) >> SHIFT)   8-bit
o_k   = sum_j W2[k][j] * h_j               12-bit signed
class = argmax o_k, lowest k on ties;  confident = best - second >= MARGIN
ood   = best < FLOOR  or  window edges < MIN_EDGES
```

**(resolved) Serial schedule and cycle count.** The spec says "one feature per cycle, 8 accumulators" but also "about 700 cycles". Eight accumulators working in parallel would finish in about 80 cycles and need 8 adders. The hardware instead evaluates **one weight per cycle into a single 16-bit accumulator**: 576 cycles for layer 1 (the ReLU and shift happen as each hidden unit completes), 64 for layer 2 and 8 for the argmax. That is **648 cycles**, 13 us at 50 MHz, with one adder.

**(resolved) Between the layers** the spec had no scaling step. `SHIFT` (host-set, 0..15) requantises the hidden units to 8 bits so layer 2 fits in 12 bits.

**(resolved) Thresholds.** The spec's training step mentions "integer thresholds" without saying where they go. They are one 8-bit signed threshold per hidden unit, B[j]: the accumulator starts at B[j] instead of 0. That is 64 more configuration bits, and it made training more stable across restarts.

**Host configuration:** the 4 pin selects, the filter mode, `SHIFT`, `MARGIN` (8-bit), `FLOOR` (12-bit signed) and `MIN_EDGES` (activity gate, 0..256).

## Weight chain

- There are 640 weights at 2 bits each: `00` = 0, `01` = +1, `11` = -1. `10` is reserved (the loader rejects it; the hardware treats it as 0).
- Order: W1[0][0..71], W1[1][0..71], ..., W1[7][0..71], W2[0][0..7], ..., W2[7][0..7]. Weight n sits in bits 2n+1:2n.
- The 8 thresholds B[0..7] follow as signed bytes: 1,344 bits in total.
- The chain is sent as 168 bytes, least significant first, and stored as hex text with 16 bytes per line (`ear/weights/*.hex`).
