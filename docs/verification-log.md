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
| Ear RTL vs the Python reference (cocotb) | 1 | | | 1 | | 2 |
| Building and measuring (synthesis, timing, datasheets) | 4 | | | 1 | | 5 |
| Gate-level simulation (CI, and locally on the Yosys netlist) | | | | 1 | 1 | 2 |
| Formal properties of the Event Machine (SymbiYosys, 8 Oct) | | | | 0 | | 0 |
| Audit of README, spec and datasheet claims against the evidence (5-8 Oct) | 10 | | | | | 10 |

The two zeros are real results. The prover has proved every shipped program and has only rejected variants broken on purpose. The Event Machine RTL matched the ISS the first time the differential test ran; the failures on the way were in the test harness. The Ear RTL did not: its first version passed every class-level test but divided by the wrong length, which only the feature readback caught (below). Both methods are sharp, going by the mutation results below, so the zeros say the ISS-level work before them was careful, not that the methods are blind. The formal zero is the third of its kind: the 27 Event Machine properties held on the existing RTL the first time they closed, and 31 planted bugs show they are not blind (below).

## Entries

Spec / ISA
- ISA v1.0 against the spec: 18 disagreements, among them a WAIT timeout register that did not exist, an undefined T after a timeout, an ADDT range too short for 9600 baud at 50 MHz, and a UART example that was late by construction (`ADDT 0, now` then `OUT @T+0`; now an ISS regression test). All resolved in ISA v1.1 (docs/isa-decisions.md).
- ISS: the v1.0 claim that UART, SPI and I2C each fit in 12 words held only for UART transmit (measured: RX 17, SPI master 19, I2C master 32).
- Building: host SPI at core clock / 4 is not reachable by a slave that synchronises SCK into the core clock; the RTL and spec now say / 8.
- Measuring: the SRAM rule assumed one single-port macro could serve two EMs, the Ear and the recorder at once (spec section 8).
- Measuring: the die is 0.92 mm2, not 0.7, and the cell budget of 1,000 cells per tile is about half what it holds (spec section 8).
- Measuring: the per-block cell estimates were about 65% low (program memory 2,921 cells, estimate 1,600).
- Place and route: the first Event Machine RTL missed 50 MHz at the slow corner by 5.0 ns, then by 2.3 ns; closed on the third run (spec section 8). Counted here as a design finding, not a functional bug.
- Place and route with the Ear: the slow corner failed only on paths from the rst_n pin into the Event Machine's operand negator. The first fix missed the real cause, which the synthesised netlist showed: a reset term in the select of a multiplexer that synthesis had placed in front of a shared subtractor. Found by reading the timing report and tracing the cells back to RTL names; the check that confirmed the fix is a longest-path count from rst_n over the netlist (22 cell levels before, 8 after). A design finding, not a functional bug.
- Place and route, third run: the remaining slow-corner paths came from synthesis merging each Event Machine's two program-memory reads into one port (and two shifters into one), reported in the Yosys log by its resource-sharing pass. Found by mapping the post-route timing report's cells back to the synthesis netlist and seeing the RX register depend on the program memory, which the RTL does not do. The carry-free comparators that replaced the negated operand were mutation-tested (a wrong carry term at bit 1 of the "one cycle away" test, and a dropped bit in the "now" test): the RTL-vs-ISS differential test catches both. A design finding, not a functional bug.

Docs (audit of the claims against the evidence, 5 Oct; fixed 8 Oct)
- SymbiYosys properties on the scheduler, capture and FIFOs were described as part of the verification; none existed. 27 Event Machine properties now do; capture is still planned.
- "The RTL is tied to the ISS by bounded formal equivalence": not built; the link is differential testing plus the properties.
- `shruti load` "refuses an unproven program", and `shruti teach`: neither command exists yet.
- An FPGA prototype "proven against" real devices, with a badge for a workflow that never ran: no FPGA build exists.
- The Ear "flags traffic it does not recognise": the out-of-distribution flag misses foreign and held-out traffic (ear/REPORT.md).
- "A 9600-baud UART and a 1-Mbaud UART produce the same interval histogram": true of the features, but at 9,600 baud a window holds about 6 edges and is gated.
- "An LLM red team whose bug yield is measured": planned, not measured.
- "Every protocol program ships with a machine-checked proof": the SPI slave does not have one.
- Stale numbers: ~10,900 cells estimated (27,789 synthesised), 28% utilisation (68.6%), and "Next: the Ear" after the Ear was built.
- "Firmware library with cocotb tests against bus models": the firmware is tested on the ISS against peer models; only UART transmit runs on the RTL.

Ear evaluation beyond the test set (ear/evaluate.py, 8 Oct). Design findings, not bugs; ear/REPORT.md has the numbers.
- Slow buses get no answer: a window closes after 65,536 cycles, so UARTs at 19,200 baud and slower are gated on 96-100% of windows. Found by counting the windows dataset.build drops.
- Fast UARTs are classified CAN on 84-95% of answered windows from 115,200 baud up; a float network on the same features reaches 83% UART recall against the integer model's 19%, so this is training, not features.
- The out-of-distribution flag misses foreign signals and held-out classes; the prototype-distance alternative flags 15% of held-out JTAG/SWD at 5% false alarms. The confident flag is the signal to trust, though it fires on 6% of WS2812 windows when the unused pins idle low and on 43% of held-out JTAG/SWD (as SPI).

Firmware (all found by running on the ISS against a peer model)
- SPI master: two OUTs scheduled at the same T set LATE; fixed with `ADDT 3, now`.
- I2C master: level WAITs on SCL matched a stale level after clock stretching; fixed with edge waits. The prover now rejects the level-wait variant as well.
- SPI slave: `OUT miso @T+1` was late; fixed with `@T+2`, with the measured limits in the spec.

ISS
- Host TX bytes were ordered by value instead of arrival cycle.
- HALT was missing from the execution log.
- The end-of-run T_PASSED differed between event-skipping and every-cycle modes.

Ear RTL (found by reading all 72 features back over SPI, window by window, against ear/features.py)
- The time-high dividers divided by the window length plus one. The window-length counter also ticked in the closing cycle, and the divider then added one more. The quotient usually floors to the same value, so every class-level test passed. A window with a pin high for exactly half its length gave 31 instead of 32.

Ear RTL (found by gate-level simulation of the Yosys netlist, before any push)
- The multiply-stage counters had no reset. RTL simulation passed, but at gate level the feature counter stayed unknown (X) through the first inference, and the class outputs went X. They are now reset. A side-by-side RTL-versus-netlist testbench then matched cycle for cycle over five windows.

Spec (found while building the Ear)
- ear/SPEC.md said one serial divider, 6 cycles per pin, fits in the 648-cycle inference. It does not: the canonical sort needs every fraction before the first multiply. The RTL uses four dividers in parallel, and ear/SPEC.md now says so.

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

The two FIFO bugs were first missed by the random test, which is why the directed tests exist.

Timing restructure of the EM (registered operand, carry-select future test, delayed RX byte write), against the same suite:

| Planted bug | Caught by |
| --- | --- |
| Operand of the next word decoded with the old P (SET P then ADDT P) | random differential (T) |
| "Due next cycle" compared against the wrong register | random differential (registers) |
| Carry-select sign ignores the low half's carry | random differential (LATE) |
| Pending RX byte not forwarded to a host read | directed test (rx_fifo_read_in_the_cycle_after_a_push) |

The forwarding bug passed every random case: no random host read lands in the one cycle that matters, which is why that directed test exists.

Ear RTL (src/shruti_ear.v), against the `ear_` tests:

| Planted bug | Caught by |
| --- | --- |
| Histogram merge drops bin 7 - s | window-by-window features |
| Suffix-sum index off by one | window-by-window features |
| Histogram shift keeps bin j = s | HOLD readback |
| Near window 3 cycles instead of 4 | class-level (random weights) |
| In-burst limit 7 half-octaves instead of 6 | HOLD readback |
| Longest interval not restarted after a large drop | class-level (random weights) |
| hi counts ignore a same-cycle change of a | class-level (random weights) |
| Argmax prefers the later class on a tie | class-level, and the delayed window |
| Rank tie broken by the higher pin number | window-by-window ("delayed": equal keys, asymmetric pairs) |
| Divider compares with > instead of >= | window-by-window (high time L / 2) |
| Divisor one less, and one more, than the length | window-by-window (tuned high times) |
| Pause one cycle short | class-level (result timing) |

Restructured Ear pause (radix-4 dividers, fetch stage, argmax stage):

| Planted bug | Caught by |
| --- | --- |
| Divider never picks digit 3 | class-level (confidence) |
| Divider's 2 x length off by one | class-level |
| Argmax stage prefers the later class on a tie | window-by-window ("delayed") |
| The "pin always high" special case removed | not caught: equivalent (digits 3, 3, 3 already give 63), so the special case was deleted |

The divider and rank mutants passed every class-level test at first. The "delayed" windows, with pin 3's high time tuned to each divider corner, were written to catch them, and on their first run they found the real divisor bug above. Firmware mutations against the prover (level wait, SDA changing with the SCL fall, inverted ACK, broken UART variants) are in prove/tests.

Formal properties of the Event Machine (verify/mutate.py, 8 Oct): 31 planted bugs in src/shruti_em.v, each run through the bounded check (em.sby, task bmc). All 31 give a counterexample, and each of the 27 properties is the first to fail for at least one of them. The table, with the failing property and the time for each, is in verify/README.md.
