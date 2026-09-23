## How it works

Shruti is a protocol emulator that also listens. One hardware stream of filtered, timestamped pin edges feeds three consumers: two Event Machines that speak UART, SPI and I2C from firmware (every timing-critical instruction is anchored to the timestamp of the last matched edge, not to when the instruction ran); Watch, a set of rule monitors with a 32-event flight recorder; and the Ear, an on-die ternary classifier that identifies an unknown bus from its edge timing, flags traffic it does not recognise, and can be retrained after fabrication over the host SPI.

Every protocol program ships with a machine-checked proof that it meets its timing contract. The full design is described in `docs/architecture-spec.md`.

This revision of the RTL (milestone 1) has both Event Machines, the 24-bit timestamp counter, the input path (two-flop synchroniser and a glitch filter with a selectable depth: bypass, 2-of-3 or 3-of-5 majority) on all 12 watchable pins, the pin drivers with per-pin open-drain mode, and the host SPI. Watch and the Ear come next; their outputs read 0.

## How to test

Connect an SPI master (mode 0, SCK at most the core clock / 8) to `ui[0]` SCK, `ui[1]` MOSI, `ui[2]` CS_n and `uo[0]` MISO. A transaction is a command byte (`0x00` write, `0x80` read), an address byte and data bytes; `docs/host-interface.md` has the register map. To send UART bytes on `uio[0]`: write the 12 words of `fw/uart_tx.s` to program addresses 0x00-0x17 (low byte first), the bit period in core cycles to 0x82-0x83 (e.g. 434 for 115200 baud at 50 MHz), the bytes to the TX FIFO at 0x86, and 1 to 0x80 to start. `uo[1]` goes high if an Event Machine halts. `test/test.py` does exactly this in cocotb and checks the pin against the instruction-set simulator cycle for cycle.

Final design: the demo board's RP2040 acts as host over the 3-wire SPI on `ui[2:0]` and `uo[0]`, loads the Event Machine programs and the Ear's weights, and reads back features and the flight recorder. The identified protocol class appears on `uo[6:4]`, so the on-board LEDs show it with no host software.

## External hardware

None for the baseline. I2C needs external pull-ups (bus pins support open-drain mode). Low-speed USB needs the external 1.5 kohm pull-up. 10BASE-T (stretch) needs series resistors and a MagJack transformer.
