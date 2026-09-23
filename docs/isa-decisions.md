# ISA decisions: v1.0 draft to v1.1

The v1.0 draft of the Event Machine ISA existed twice: in the architecture spec's ISA section and in `isa/isa.yaml`. The two disagreed in 18 places. This note lists each disagreement and how v1.1 resolves it. `isa/isa.yaml` is normative; the spec's ISA section summarises it.

## Disagreements and resolutions

| # | Topic | v1.0 spec | v1.0 isa.yaml | Problem | v1.1 resolution |
|---|---|---|---|---|---|
| 1 | OUT@ delay | d = 8 bits x prescale (1/4/16/64) | 4-bit count + 2-bit prescale; header comment says 8-bit count x prescale | The spec form needs 4+4+2+8+2 = 20 bits and cannot fit a 16-bit word; the yaml contradicted itself | d is a plain 6-bit count, 0..63 cycles, no prescale. Longer delays use ADDT, which keeps timing drift-free and the prover's arithmetic linear |
| 2 | IN@ delay | same d as OUT@ | 8 bits, no prescale | OUT@ and IN@ used different delay formats | IN@ uses the same 6-bit d as OUT@ |
| 3 | IN@ d = 0 | reads the edge snapshot | same | After ADDT moves T, a live sample at exactly T was impossible because d = 0 was taken | Explicit `snap` bit: `IN pin @snap` reads the snapshot, `IN pin @T+0` samples live at T |
| 4 | JMP pin | pin high or low, 12 pins | 3-bit pin field | 3 bits reach only 8 pins; M0-M3 were unreachable | JMP = cond 3 + pin 4 + target 5 |
| 5 | JMP conditions | 9 conditions incl. FIFO empty, FIFO full, slot busy | 4-bit cond | Which FIFO "empty/full" referred to was undefined | Five condition codes: flag, X--, Y--, high, low. Code "flag" selects through the pin field: always, TO, LATE, TXE (TX FIFO empty), !OSRE (OSR not empty). "FIFO full" is dropped (blocking PUSH covers it), and so is "slot busy" (blocking OUT and the ADDT-now rule cover it). LATE was added at review so firmware can test it |
| 6 | ADDT range | `ADDT B`, B up to 65,535 | d = 11 bits (max 2,047) | 9600 baud at 50 MHz is 5,208 cycles and did not fit | Per-EM 16-bit period register P, host-written (EMx_P) or loaded by `SET P, imm10`. New opcode `ADDT P[/2|/4|/8] [, now]`; immediate ADDT keeps its 11 bits. Rates slower than 763 baud at 50 MHz load P with 1/k of the period and take k steps per bit (300 baud: P = 55,556, k = 3). Two `ADDT P/2` steps equal one `ADDT P` and do not extend the range |
| 7 | ADDT now | T := counter + d | same | Re-anchoring while slots were pending could move T backwards and clip the previous stop bit | `ADDT d, now` is T := max(T, counter + d), with a T_PASSED bit guarding against counter wrap. The exact rule is in isa.yaml |
| 8 | SHIFT | "shift OSR out or count ISR bits" | dir, n, auto | Unclear whether it was an action or configuration; no bit order, although UART is LSB-first and SPI/I2C are MSB-first | SHIFT is configuration only. It writes the CFG register: bit order, word length and the autopull/autopush threshold per direction |
| 9 | OUT value OSR | "data bit, then SHIFT out 1" | value 2 = next OSR bit | Whether OUT consumed the bit was unstated | OUT with value = OSR consumes exactly one bit per execution. Value 3 (~OSR) drives the complement of the next bit without consuming it, for differential pairs |
| 10 | OSR/ISR width | 16 bits | not stated | FIFOs are 8 bits wide; alignment was undefined | OSR and ISR stay 16 bits (9-bit UART frames, 16-bit SPI, JTAG, USB CRC16). Words are right-justified, n = 1..16 bits; PULL and PUSH move 1 byte for n <= 8, else 2 bytes, low byte first |
| 11 | SET immediate | `SET X, imm` | 10 bits | The load range was unstated | Stated: 0..1023, zero-extended. SET gains target P |
| 12 | WAIT timeout | a timeout register in per-EM state | 6-bit immediate, 2^n | Only n <= 23 is meaningful for a 24-bit counter; the register did not exist in the yaml; T after a timeout was undefined | 5-bit field: 0 = none, n = 1..23 gives 2^n cycles after T. On timeout, TO := 1 and T := the deadline, so later timing stays anchored. There is no timeout register |
| 13 | WAIT edge | "edge" | rise, fall, any | A WAIT issued after the edge had already happened hung (e.g. I2C clock-stretch release) | Mode is 3 bits: rise, fall, any, high, low. Level modes match immediately |
| 14 | SYNC | set n / wait n | op + flag | Whether wait cleared the flag was unstated | Wait blocks until the flag is set, then clears it |
| 15 | HALT | absent | opcode 15 | Missing from the spec | Added to the spec |
| 16 | Opcode 0 | - | WAIT | Program memory resets to zero, so an unloaded EM would sit in WAIT forever | HALT is opcode 0. Opcodes 12-15 and illegal operands also halt |
| 17 | Timing model | - | - | Cycles per instruction, arming for a past time, 24-bit wrap and "before" were all undefined | One cycle per instruction unless blocked. Modular time with a 2^23 future window. A late OUT@ or IN@ sets the sticky LATE flag, which is readable by JMP and the host; every contract requires LATE never set, and the prover treats it as a hard error |
| 18 | UART example | "seven instructions" | - | Listed eight; never set X; relied on an implicit OSR shift; did not loop; `ADDT 0, now` then `OUT @T+0` was late by construction | Replaced by a 12-word program (spec section "Event-native datapath and ISA"), marked as traced by hand until the ISS runs it. The late case becomes an ISS test |

## Other choices made at the same review

- OSR and ISR kept at 16 bits; 32 flops per EM is cheap for what the width buys.
- The ISA is edited in place with `version: "1.1"`; git history is the version record.
- Estimated cost of the additions per EM: P (16 flops), CFG (12 flops), T_PASSED and flags (5 flops), a 16-bit 4:1 mux for P >> s, and the ADDT-now max comparator. That is roughly 150 cells per EM, pre-synthesis, against the 1,200-cell EM estimate.
