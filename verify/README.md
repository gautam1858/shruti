# verify: formal properties and the signoff gate

What exists today (8 Oct 2026):

- `em.sby`: SymbiYosys setups for the Event Machine's properties, which sit in the `ifdef FORMAL` block at the end of `src/shruti_em.v`.
- `mutate.py`: plants one bug at a time in a copy of the RTL and checks that the bounded run fails.
- `signoff.py`: the CI gate on the post-route metrics (the `signoff` job in `.github/workflows/gds.yaml`).

The `formal` workflow runs all three formal steps on every push.

The constrained-random differential harness (RTL against the ISS) and the Ear's bit-exact tests are cocotb tests in `test/test.py`. Planned here:
- formal properties for edge capture, and for the flight recorder once Watch is built;
- the classifier proof (spec milestone 13 Dec 2026);
- a bounded equivalence check of the Event Machine against a Verilog transliteration of the ISS;
- cocotb bus models with protocol-level mutations.

## Running

```
cd verify
sby -f em.sby prove      # k-induction, about 2 minutes with z3
sby -f em.sby cover      # every cover statement is reachable, seconds
python3 mutate.py        # every planted bug fails the bounded check (bmc, depth 20), about 17 minutes
python3 signoff.py ../runs/wokwi/final/metrics.json --config ../src/config.json
```

The CI job installs the OSS CAD Suite; locally any Yosys with SymbiYosys and z3 works. Open-source Yosys has no SVA front end, so the properties are immediate assertions with `$past` under `ifdef FORMAL`. The flow and the simulators never define `FORMAL`, so the synthesised netlist does not change.

## Event Machine properties

Setup. `shruti_em` is the top. Program memory, pin levels, SYNC flags and every host strobe are free inputs. The environment assumes only what `src/project.v` guarantees:
- reset in the first cycle;
- `run` low while reset is held and in the cycle after;
- a counter that steps by one every cycle;
- `vis_prev` is last cycle's `vis`.

The times the properties check are computed in the FORMAL block from `isa/isa.yaml` (T + d, T + 2^n and the modular "future" of `timing.future`). They are not taken from the RTL's `dT` register or its carry-free comparators, so the properties check those.

Status: all 27 are **proved by k-induction** (depth 4, z3, about 2 minutes). Every cover statement is reached within 5 cycles, so none of the properties is vacuous.

| ID | Property | Source |
| --- | --- | --- |
| T1 | `dT` equals T minus the counter in every cycle | RTL timing structure |
| T2 | while T_PASSED is clear, T is not in the past | isa.yaml `t_passed` |
| T3 | the registered time operand matches the fetched word and P | RTL timing structure |
| S1 | a busy scheduler slot's time is never already past (so it fires) | spec 9.4 "never late" |
| S2 | an OUT on time takes effect at exactly T + d (a slot armed for T + d, or the next cycle when T + d is the next cycle) and leaves LATE alone | isa.yaml `OUT`, `late` |
| S3 | an OUT whose target is not in the future sets LATE and takes effect the next cycle | isa.yaml `late` |
| S4 | an OUT blocks while both slots are busy | isa.yaml `OUT` |
| S5 | an armed slot holds T + d, the pin and the value | isa.yaml `OUT` |
| S6 | a slot fires once: it is free the cycle after it fires | spec 9.4 "never twice" |
| S7 | a firing slot drives its pin, at its value unless a later-armed slot or this cycle's instruction drives the same pin | isa.yaml `OUT` |
| I1 | IN@ in its first cycle samples at once when the target is now or past (setting LATE only when past), and otherwise waits | isa.yaml `IN`, `late` |
| I2 | a waiting IN@ samples at exactly T + d, and its target never passes while it waits | isa.yaml `IN` |
| W1 | a WAIT with a timeout times out exactly when its deadline T + 2^n stops being in the future, and the deadline never passes while it waits | isa.yaml `WAIT` |
| W2 | a timeout sets TO and moves T to the deadline | isa.yaml `WAIT` |
| W3 | a match wins over the timeout; T becomes the edge time and SNAPSHOT the pin levels | isa.yaml `WAIT` |
| F1 | both FIFO counts stay within 0..4 | isa.yaml `fifo_depth` |
| F2 | a pull never takes more bytes than the TX FIFO holds | spec 9.4 "no underflow" |
| F3 | a push never overfills the RX FIFO | spec 9.4 "no overflow" |
| F4 | a non-blocking PUSH that cannot fit sets OVF and clears the ISR; a blocking one waits | isa.yaml `PUSH` |
| F5 | IN with autopush never completes without pushing | isa.yaml `IN` |
| F6 | PULL takes the word when it is there, a blocking PULL waits, a non-blocking one leaves the OSR alone | isa.yaml `PULL` |
| F7 | OUT value=OSR with the OSR empty sets UNF without consuming a bit, or with AUTOPULL waits for data | isa.yaml `OUT` |
| F8 | the host side counts as documented: a write to a full TX FIFO is dropped unless the EM pulls in the same cycle; a read of an empty RX FIFO changes nothing | docs/host-interface.md |
| H1 | an EM halts exactly on opcodes 0 and 12-15 and on every illegal operand isa.yaml lists | isa.yaml `HALT` |
| H2 | halting, and being halted, has no side effect: no fetch, FIFO access, slot, pin drive, T write or SYNC change | isa.yaml `HALT` |
| H3 | HALT is sticky until the host stops the EM, and the PC stays put | isa.yaml `HALT` |
| H4 | the PC steps by one, wrapping 31 to 0, or jumps to the encoded target; it is 0 while stopped | isa.yaml `program_words`, `JMP` |

Two limits:
- **Host-side drops.** The host's write to a full TX FIFO is dropped silently by design (docs/host-interface.md says to poll the count first). F8 proves the documented behaviour; it does not make it loud.
- **No ISS equivalence.** The properties do not compare the Event Machine with the ISS instruction by instruction. That is the planned bounded equivalence check; today the cocotb differential test covers it on random programs.

## Planted bugs

`python3 mutate.py` prints this table. Every planted bug gives a counterexample in the bounded run, and the "Caught by" column names the property that failed.

| Mutation | Planted bug | Result | Caught by | Seconds |
| --- | --- | --- | --- | --- |
| out_one_ahead_armed | an OUT due next cycle is armed as a slot (it can never fire) | caught | S1 | 3 |
| one_tgt_carry | the carry-free 'one cycle away' test uses the wrong carry at bit 1 | caught | S1 | 3 |
| out_late_at_now | an OUT whose target is the current cycle does not set LATE | caught | S3 | 2 |
| slot_never_frees | a slot stays busy after it fires | caught | S1 | 6 |
| in_waits_one_short | a waiting IN@ samples one cycle before its target | caught | I2 | 4 |
| wait_timeout_early | a waiting WAIT times out one cycle before its deadline | caught | W1 | 3 |
| wait_match_dt | WAIT's match sets dT to 0 instead of T - (counter + 1) | caught | T1 | 3 |
| rx_overflow | a one-byte push is allowed into a full RX FIFO | caught | F3 | 17 |
| tx_underflow | a two-byte pull is allowed with one byte in the TX FIFO | caught | F2 | 2 |
| push_silent_drop | a non-blocking PUSH into a full RX FIFO drops the word without OVF | caught | F4 | 21 |
| tx_room_before_pull | the TX FIFO judges room before this cycle's pull, not after | caught | F8 | 457 |
| unf_missing | OUT value=OSR with the OSR empty and no autopull does not set UNF | caught | F7 | 2 |
| autopull_no_block | OUT with autopull goes ahead without TX data | caught | F2, F7 | 2 |
| illegal_timeout | WAIT timeout 24 is accepted instead of halting | caught | H1 | 2 |
| halt_not_sticky | HALT does not latch | caught | H3 | 5 |
| addt_now_tp | ADDT now with d = 0 clears T_PASSED although T is the current cycle | caught | T2 | 2 |
| operand_ignores_host_p | the fetched operand ignores a host write to P's high byte in the same cycle | caught | T3 | 2 |
| out_even_d_now | an on-time OUT with an even target takes effect next cycle, not at T + d | caught | S2 | 2 |
| out_both_busy | OUT arms over a busy slot when both slots are busy | caught | S4 | 42 |
| slot1_wrong_pin | slot 1 records the SET pin field instead of the OUT pin | caught | S5 | 64 |
| slot1_double_fire | slot 1 stays busy when both slots fire in the same cycle | caught | S1 | 57 |
| slot_refires | a slot that fires re-arms itself two cycles later instead of freeing | caught | S6 | 64 |
| slot_drive_dropped | slot drives are dropped while slot 1 is busy | caught | S7 | 8 |
| in_first_early | IN@ in its first cycle samples when the target is one cycle away | caught | I1 | 3 |
| wait_timeout_no_to | a WAIT timeout does not set TO | caught | W2 | 7 |
| wait_match_snapshot | a WAIT match latches last cycle's pin levels into SNAPSHOT | caught | W3 | 6 |
| tx_host_overflow | a host write to a full TX FIFO is accepted | caught | F1 | 198 |
| autopush_drops | IN with autopush completes without pushing when the RX FIFO is full | caught | F5 | 15 |
| pull_nb_resets_osr | a non-blocking PULL with no data resets OSR_COUNT | caught | F6 | 2 |
| illegal_set_drives | SET on an input-only pin halts but still drives a pin | caught | H2 | 2 |
| pc_no_wrap | the PC sticks at 31 instead of wrapping to 0 | caught | H4 | 76 |

All 31 are caught, and each of the 27 properties is the first to fail for at least one of them. Seconds are wall-clock time on one core with z3.

## Signoff gate

`signoff.py` reads LibreLane's `runs/wokwi/final/metrics.json` from the `GDS_logs` artifact. It is needed because the `gds` job passes even when timing fails.

It fails on:
- negative setup or hold slack, or any violation, at any corner;
- routing DRC, antenna, Magic DRC or LVS errors;
- a missing metric.

It warns when:
- the slow-corner setup slack is under +4.0 ns;
- the worst hold slack is under `GRT_RESIZER_HOLD_SLACK_MARGIN` (`src/config.json`);
- there are max-slew, max-cap or max-fanout violations.

Its table goes to the job summary. `tests/test_signoff.py` runs it on the real metrics of the GDS run for commit fcafc0b, and on a doctored copy that must fail.
