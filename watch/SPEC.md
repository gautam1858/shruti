# Watch: rule monitors, trigger and flight recorder (cut level)

Watch observes the same filtered pin stream as the Event Machines and the Ear and never drives
the bus. This is the cut level of `docs/architecture-spec.md` ("Watch"): 2 rule monitors and a
16-entry recorder. `watch/model.py` is the cycle-exact reference; `src/shruti_watch.v` is the
RTL, tested against it in `test/test.py`.

## The stream

In cycle t Watch sees `vis[t]`, the filtered level of the 12 watchable pins (B0-B7, M0-M3,
the same `filt_d` the Event Machines read), and `vis[t-1]`. An edge on pin p in cycle t is
`vis[t][p] != vis[t-1][p]`. `cnt[t]` is the 24-bit timestamp counter.

## Rule monitors (2)

Monitor m matches in cycle t when all of these hold:

- its edge selector is on (rise, fall or any) and pin `PIN` has that edge in cycle t;
- the guard: `vis[t] & MASK == VALUE & MASK` (the levels after the edge, so "edge on A while
  B is high and C low");
- the interval since that pin's previous edge, `I`, has `CMIN <= log2_code(I) <= CMAX`, with
  `log2_code` the Ear's 6-bit code (`ear/features.py`: leading-one position and the next two
  bits). `I` saturates at 65,535 cycles, and an edge with no earlier edge since reset or since
  `CFG` was last written has `I` = 65,535. The defaults, 0 and 63, pass every interval.

A match adds one to the monitor's 8-bit hit counter (saturating at 255), and, if its actions
say so, is a trigger source and sets SYNC flag `SYNCIDX` for the Event Machines in the next
cycle (as if an EM had set it).

Examples: a UART pulse shorter than half a bit (edge on RX with `CMAX` below half a bit's
code), an SPI data edge right after the sampling clock edge (edge on MOSI with SCK high, and
the interval since SCK's edge measured on a monitor of SCK), a USB SE0 longer than two bit
times.

## Trigger

Sources, each enabled in `SRC`: monitor 0, monitor 1, the Ear's OOD flag rising, a change of
the Ear's class pins (`uo_out[6:4]`), and a rising edge on trigger-in (`ui_in[3]`, through
two synchroniser flops). Setting `TRIG` in `CTRL` triggers from the host. A trigger counts
only while Watch is armed and not yet triggered; `CAUSE` keeps the enabled sources that fired
in the trigger cycle and `TTIME` its timestamp.

From the cycle after the trigger, trigger-out (`uo_out[2]`) is high until the host writes
`CTRL`, so a scope sees the moment.

## Recorder (16 entries)

While armed and not frozen, every cycle with an edge on a pin in `PINS` writes one entry:
`{vis[t], cnt[t][15:0]}`, 28 bits, the levels after the edge. Edges in the same cycle share
one entry, so nothing is lost when two pins change together; the pins that changed are the
difference from the entry before. The recorder is a ring: after 16 entries the oldest goes.

On the trigger, the entry of the trigger cycle (if it has one) is kept and the recorder takes
`POST` more entries (0-15), then freezes. A frozen recorder raises IRQ (`uo_out[1]`, shared
with "an Event Machine halted") until the host writes `CTRL`.

The host reads the entries oldest first through the `DATA` port, 4 bytes each: timestamp low
and high byte, levels B0-B7, levels M0-M3 in bits 3:0. Reading past the last valid entry
returns 0; writing `DATA` restarts at the oldest entry.

## Registers

| Address | Name | Access | Meaning |
| --- | --- | --- | --- |
| 0xE0 | CTRL | R/W | Write with bit 1 TRIG set: trigger now, if armed; nothing else changes. Otherwise bit 0 ARM: 1 clears the recorder and arms, 0 disarms (the entries stay), and either clears TRIGGERED, FROZEN, IRQ and trigger-out. Reads {FROZEN, TRIGGERED, ARMED} |
| 0xE1 | SRC | R/W | Trigger sources: bit 0 monitor 0, 1 monitor 1, 2 Ear OOD rising, 3 Ear class change, 4 trigger-in rising |
| 0xE2 | POST | R/W | Entries kept after the trigger, 0-15 |
| 0xE3 | CAUSE | R | The SRC bits that fired in the trigger cycle |
| 0xE4 | COUNT | R | Valid entries, 0-16 |
| 0xE5-0xE7 | TTIME | R | Timestamp of the trigger cycle, 24 bits, low byte first |
| 0xE8, 0xE9 | PINS | R/W | Pins whose edges are recorded, B0-B7 in 0xE8, M0-M3 in 0xE9[3:0]; all after reset |
| 0xEA | DATA | R / W | Read: the next recorder byte, oldest entry first. Write: restart |
| 0xEC, 0xED | HITS | R / W | Monitor 0 and 1 hit counters; a write clears |
| 0xF0 + 8m | MON_CFG | R/W | Monitor m: bits 3:0 PIN (0-11 = B0-B7, M0-M3), 5:4 edge (0 off, 1 rise, 2 fall, 3 any), 6 trigger source, 7 set a SYNC flag. A write restarts the interval |
| 0xF1 + 8m | MON_SYNC | R/W | Bits 1:0 SYNCIDX |
| 0xF2, 0xF3 + 8m | MON_MASK | R/W | Guard mask, 12 bits |
| 0xF4, 0xF5 + 8m | MON_VALUE | R/W | Guard value, 12 bits |
| 0xF6 + 8m | MON_CMIN | R/W | Shortest interval code, 0-63 (0 after reset) |
| 0xF7 + 8m | MON_CMAX | R/W | Longest interval code, 0-63 (63 after reset) |

## Differences from the full Watch in the architecture spec

- 2 monitors and 16 entries, not 4 and 32 (the cut level).
- An entry is a 12-pin snapshot and a timestamp (28 bits), not a pin number and an edge bit
  (21 bits): same-cycle edges, such as an SPI clock and data changing together, would
  otherwise lose one of them.
- No "halt the Event Machines" action. Holding the EMs needs a pause input to the Event
  Machine that it does not have (`RUN` = 0 resets it), so the actions are count, trigger and
  SYNC flag; a SYNC flag lets firmware stop itself.
