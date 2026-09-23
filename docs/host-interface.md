# Host interface

The host (the demo board's RP2040, or any SPI master) loads programs, sets up and starts the Event Machines, feeds their TX FIFOs and drains their RX FIFOs over a 3-wire SPI port. RTL: `src/shruti_spi.v` and the register map in `src/project.v`. Tests: `test/test.py` through the harness in `test/harness.py`.

## Pins

| Pin | Signal |
| --- | --- |
| ui_in[0] | SCK |
| ui_in[1] | MOSI |
| ui_in[2] | CS_n |
| uo_out[0] | MISO |
| uo_out[1] | IRQ: high while any Event Machine is halted |

## SPI protocol

Mode 0 (SCK idles low, data sampled on the rising edge), MSB first. The chip synchronises SCK, MOSI and CS_n into its clock domain and detects SCK edges there, so **SCK must be at most core clock / 8** (6.25 MHz at 50 MHz). Every test runs at exactly core clock / 8; faster rates are not claimed.

A transaction is CS_n low, then:

1. a command byte: `0x00` write, `0x80` read (bits 6..0 are reserved, send 0);
2. an address byte;
3. any number of data bytes.

The address increments after each data byte, except at the FIFO ports (`+6` and `+7` below), where it stays put so a burst goes to the same FIFO. CS_n high ends the transaction.

On a read, MISO changes shortly after each SCK rising edge, and the master samples it on the next rising edge. A read of the RX FIFO port removes a byte only when the host clocks that byte's first bit, so ending a burst early never loses data.

## Register map

| Address | Name | Access | Contents |
| --- | --- | --- | --- |
| 0x00-0x3F | PROG0 | W | EM0 program, 32 words. Low byte at the even address, high byte at the odd one; the word is written when its high byte arrives |
| 0x40-0x7F | PROG1 | W | EM1 program |
| 0x80 + n | EM0 | | EM0 registers, below |
| 0x90 + n | EM1 | | EM1 registers, below |
| 0xA0 | SYNC | R/W | The four SYNC flags, bits 3..0. A write sets all four |
| 0xA1 | FILTER | R/W | Input filter for all 12 pins: 0 bypass, 1 2-of-3 majority, 2 3-of-5 majority |
| 0xA2 | RELEASE | W / R | Write: stop driving the B pins set in the mask. Read: the B pins' output enables |
| 0xA3 | LEVELS | R | Filtered levels of B0-B7 |
| 0xA4 | ID | R | 0x53 ("S") |
| 0xA5 | VERSION | R | ISA version, 0x11 for v1.1 |
| 0xA6 | RUN_ALL | R/W | RUN for both EMs, bit 1 EM1 and bit 0 EM0, so both start in the same cycle |

Per Event Machine (offset from 0x80 or 0x90):

| Offset | Name | Access | Contents |
| --- | --- | --- | --- |
| +0 | CTRL | R/W | Bit 0 RUN. Reads {HALTED, RUN} |
| +1 | FLAGS | R / W1C | Reads {UNF, OVF, LATE, TO}. Writing 1 to bit 1, 2 or 3 clears LATE, OVF or UNF |
| +2, +3 | P | R/W | Bit period P, low byte then high byte |
| +4 | CFG_OUT | R/W | {AUTOPULL, OUT_MSB, OUT_N[3:0]} |
| +5 | CFG_IN | R/W | {AUTOPUSH, IN_MSB, IN_N[3:0]} |
| +6 | TXFIFO | W / R | Write: push a byte. Read: the TX FIFO byte count |
| +7 | RXFIFO | R | Pop a byte |
| +8 | RXCOUNT | R | RX FIFO byte count |
| +9 | PC | R | Program counter |
| +A, +B | X | R | X register |
| +C, +D | Y | R | Y register |

## Starting and stopping

RUN = 0 holds an EM in the ISA's reset state:
- PC 0, and T tracking the counter;
- X and Y 0, and all flags clear;
- OSR and ISR empty, all pins push-pull, and both scheduler slots free.

P, the CFG fields and both FIFOs keep their contents, so the host sets them up while the EM is stopped. Setting RUN starts the EM at PC 0. Clearing it returns the EM to the reset state, which also clears its sticky flags.

Chip reset leaves program memory undefined, so load all 32 words before starting an EM. Pins an EM has driven stay driven after it stops; write RELEASE to let them go.

## Timing

A register write takes effect 4 core cycles after the SCK rising edge that clocks in the data byte's last bit. That is two synchroniser flops, the edge detector and the write strobe. The first cycle of a run is therefore that edge plus 4, and the tests use this to line the chip up with the ISS cycle for cycle.

In ISS terms (`iss/sim.py`), a write or FIFO read in effect from chip cycle w is a host action at ISS cycle w, which lands before the Event Machines execute in that cycle.

**TX FIFO full.** A byte written to a full TX FIFO is dropped. It is kept if the EM pulls a byte in the same cycle the write strobe fires. `Sim(host_tx_drop=True)` models this, and `test/test.py::tx_fifo_write_in_the_cycle_of_a_pull` checks the cycles before, at and after the pull. Poll the TX count (`+6`) before writing when the FIFO may be full.
