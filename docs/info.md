## How it works

Shruti is a protocol emulator that also listens. One hardware stream of filtered, timestamped pin edges feeds three consumers: two Event Machines that speak UART, SPI and I2C from firmware (every timing-critical instruction is anchored to the timestamp of the last matched edge, not to when the instruction ran); Watch, a set of rule monitors with a 32-event flight recorder; and the Ear, an on-die ternary classifier that identifies an unknown bus from its edge timing, flags traffic it does not recognise, and can be retrained after fabrication over the host SPI.

Every protocol program ships with a machine-checked proof that it meets its timing contract. The full design is described in `docs/architecture-spec.md`.

Milestone 0 (this revision of the RTL) is the front of the event stream only: a two-flop synchroniser, a per-pin glitch filter with a selectable depth (bypass, 2-of-3 or 3-of-5 majority) and an edge detector on the eight bus pins.

## How to test

Milestone 0: set `ui[1:0]` to the filter mode (`00` bypass, `01` 2-of-3, `10` 3-of-5), drive edges on `uio[7:0]`, and watch `uo[7:0]`: each bus pin gives a one-cycle strobe on `uo` for every filtered edge. A one-cycle glitch produces two strobes in bypass mode and none in 3-of-5 mode. `test/test.py` checks exactly this with cocotb.

Final design: the demo board's RP2040 acts as host over the 3-wire SPI on `ui[2:0]` and `uo[0]`, loads the Event Machine programs and the Ear's weights, and reads back features and the flight recorder. The identified protocol class appears on `uo[6:4]`, so the on-board LEDs show it with no host software.

## External hardware

None for the baseline. I2C needs external pull-ups (bus pins support open-drain mode). Low-speed USB needs the external 1.5 kohm pull-up. 10BASE-T (stretch) needs series resistors and a MagJack transformer.
