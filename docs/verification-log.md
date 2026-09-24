# Verification log

The submission's verification report is built from this log: every defect found, where it was, and which method found it. Deliberately planted bugs (mutation tests) are counted separately; they measure how sharp a method is, not what it found. Started 23 Sep 2026.

## Bugs found, by method

| Method | Spec / ISA | ISS | Firmware | RTL | Flow | Total |
| --- | --- | --- | --- | --- | --- | --- |
| ISA review against the spec (docs/isa-decisions.md) | 18 | | | | | 18 |
| Firmware run on the ISS against peer models | 1 | | 3 | | | 4 |
| ISS self-checks (event-skipping vs every-cycle mode, logging) | | 3 | | | | 3 |
| Prover (Z3) | | | 0 | | | 0 |
| RTL vs ISS differential (cocotb) | | | | 0 | | 0 |
| Building and measuring (synthesis, timing, datasheets) | 4 | | | 1 | | 5 |
| Gate-level simulation in CI | | | | | 1 | 1 |

The two zeros are real results. The prover has proved every shipped program and has only rejected variants broken on purpose. The RTL matched the ISS the first time the differential test ran; the failures on the way were in the test harness. Both methods are sharp, going by the mutation results below, so the zeros say the ISS-level work before them was careful, not that the methods are blind.

## Entries

Spec / ISA
- ISA v1.0 against the spec: 18 disagreements, among them a WAIT timeout register that did not exist, an undefined T after a timeout, an ADDT range too short for 9600 baud at 50 MHz, and a UART example that was late by construction (`ADDT 0, now` then `OUT @T+0`; now an ISS regression test). All resolved in ISA v1.1 (docs/isa-decisions.md).
- ISS: the v1.0 claim that UART, SPI and I2C each fit in 12 words held only for UART transmit (measured: RX 17, SPI master 19, I2C master 32).
- Building: host SPI at core clock / 4 is not reachable by a slave that synchronises SCK into the core clock; the RTL and spec now say / 8.
- Measuring: the SRAM rule assumed one single-port macro could serve two EMs, the Ear and the recorder at once (spec section 8).
- Measuring: the die is 0.92 mm2, not 0.7, and the cell budget of 1,000 cells per tile is about half what it holds (spec section 8).
- Measuring: the per-block cell estimates were about 65% low (program memory 2,921 cells, estimate 1,600).
- Place and route: the first Event Machine RTL missed 50 MHz at the slow corner by 5.0 ns, then by 2.3 ns; closed on the third run (spec section 8). Counted here as a design finding, not a functional bug.

Firmware (all found by running on the ISS against a peer model)
- SPI master: two OUTs scheduled at the same T set LATE; fixed with `ADDT 3, now`.
- I2C master: level WAITs on SCL matched a stale level after clock stretching; fixed with edge waits. The prover now rejects the level-wait variant as well.
- SPI slave: `OUT miso @T+1` was late; fixed with `@T+2`, with the measured limits in the spec.

ISS
- Host TX bytes were ordered by value instead of arrival cycle.
- HALT was missing from the execution log.
- The end-of-run T_PASSED differed between event-skipping and every-cycle modes.

Flow
- The CMOS5L template's gate-level Makefile leaves out `sg13cmos5l_udp.v`, where the PDK keeps the flip-flop primitives, so every gate-level test failed to elaborate. Fixed in test/Makefile. The upstream template has the same omission.

## Mutation tests: planted bugs

RTL (src/shruti_em.v), each planted alone, against the cocotb suite:

| Planted bug | Caught by |
| --- | --- |
| Slot priority order reversed | random differential (pin trace) |
| OUT due next cycle armed as a slot instead of driven | random differential (registers) |
| WAIT timeout ignored in its first cycle | random differential (registers) |
| JMP TO does not clear TO | random differential (registers) |
| IN at a past time blocks instead of sampling | random differential (registers) |
| Open-drain mode taken from the old PIN_MODE | random differential (pin trace) |
| SYNC wait does not clear the flag | random differential (SYNC flags) |
| ISR ignores MSB-first order | random differential (registers) |
| TX FIFO refuses a write in the cycle of a pull | directed test (tx_fifo_write_in_the_cycle_of_a_pull) |
| Two-byte RX push writes the wrong slot on wrap | directed test (rx_fifo_two_byte_push_wraps) |
| ADDT now: keep T only if strictly later | not caught: equivalent (when equal, both branches give the same T) |

The two FIFO bugs were first missed by the random test, which is why the directed tests exist. Firmware mutations against the prover (level wait, SDA changing with the SCL fall, inverted ACK, broken UART variants) are in prove/tests.
