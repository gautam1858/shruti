# shruti prove v0: design note (for approval)

Goal: prove that `fw/uart_tx.s` meets its contract for every data byte and every bit period B in the supported range, in under a second, and for a broken program, return a counterexample pin trace that the ISS reproduces.

## 1. Contract format

A contract is a YAML file beside its program (`fw/uart_tx.contract.yaml`). It has three parts:
- **assume:** what the environment may do. Symbolic parameters, symbolic data, and host behaviour.
- **guarantee:** what the pin trace must be, plus the flags that must never be set.
- **bounds:** how much of the behaviour is explored.

```yaml
program: uart_tx.s
assume:
  P: {min: 8, max: 65535}           # symbolic bit period B, loaded by the host
  initial: {B0: 0}                  # external level of undriven pins
  tx_fifo:                          # symbolic bytes, symbolic arrival cycles
    - {data: d0, at: a0}            # a0 >= 0
    - {data: d1, at: a1}            # a1 >= a0; covers back-to-back and after-idle frames
guarantee:
  never: [LATE, UNF, OVF, HALT]     # LATE is a hard error in every contract
  pin: B0
  frames:                           # one frame per byte; levels hold over [start, end)
    - repeat: {for: [d0, d1], as: d}
      start: {not_before: prev_end,  # never overlaps the previous stop bit
              within: 5, after: "max(prev_end, arrival(d))"}   # response bound, cycles
      waveform:
        - {from: 0,     to: P,      level: 0}
        - {from: "k*P", to: "(k+1)*P", level: "d[k-1]", k: "1..8"}
        - {from: "9*P", to: "10*P", level: 1}
  between_frames: {level: 1}        # line idles high; no other edges on B0
bounds:
  frames: 2                         # symbolic executor explores this many bytes
```

The expression language is small: integers, the symbols from `assume`, `P`, `+ - *` by constants, `max`, and bit selection `d[k]`. Each contract compiles to one Z3 formula, so a new protocol needs a new contract file, not new prover code. A PS/2 contract fits the same shape. SPI and I2C need extra guarantee kinds: relations between pins ("MOSI stable at every SCK rise") and response bounds against a peer.

## 2. Symbolic execution of the ISS

A symbolic interpreter executes one instruction at a time, mirroring the ISS:
- **Concrete:** control state, meaning PC, X, Y, OSR_COUNT, CFG, slot occupancy and FIFO occupancy. For the programs in scope these do not depend on the data or on B.
- **Symbolic:**
  - Data are Z3 bit-vectors. OSR bits are `Extract` of the data bytes, so pin levels are bit-vector terms.
  - Times are Z3 integers: T, the current cycle, slot fire times and FIFO arrival times. They are linear in P and the arrival variables.
- **Blocking:** a blocking instruction advances the current cycle to a symbolic time. An OUT with both slots busy waits for the earliest slot to fire. A PULL waits for `max(now, arrival)`. `ADDT now` gives `max(T, now + d)`. `max` becomes a Z3 `If`, so there is no path explosion.
- **Branches:** a branch on symbolic state forks the path and adds its condition to the path constraint. Pin conditions and TO are examples. UART TX has no such branches; RX and I2C will.
- **LATE:** every OUT and IN produces an obligation `1 <= fire - now < 2^23` (or `0 <=` for IN).
- **Output:** a symbolic pin trace, a list of `(time, pin, level)` terms under a path constraint, plus the obligations.
- **Loop bounds:** loops are bounded by concrete counters. Unbounded idle loops (`JMP top`) stop after `bounds.frames` bytes.
- **Counter wrap:** times are unbounded integers. The interpreter asserts that the explored span stays under 2^23 cycles, so modular comparison and plain comparison agree. For two UART frames at B = 65,535 the span is 1.3 M, well under 8.4 M.
- **Input latency:** L is modelled as in the ISS, as a parameter.

## 3. Checking

- One query per contract: `path ∧ assumptions ∧ ¬(obligations ∧ waveform ∧ never-flags)`.
- UNSAT is a proof for all bytes and every B in range.
- SAT gives a model (B, bytes, arrival times). The prover then **re-runs the concrete ISS** with those values and reports the ISS's own pin trace next to the expected waveform, with the first differing edge marked. The counterexample therefore never depends on trusting the symbolic interpreter.
- Waveform check:
  - Pad changes happen only at scheduled drive times.
  - The interpreter emits every drive event, including drives that don't change the level.
  - The check requires that each contract interval contains no drive to the other level, and that each boundary gets the new level.
  - The final UART loop only arms OUTs at T, so this stays linear.

## 4. Trusting the interpreter

The symbolic interpreter is a second implementation of the ISA semantics. The v0 guard is a differential test in `prove/tests`: the interpreter is made concrete by substituting random B, data and arrival times, and its trace must equal the ISS trace. It runs on every firmware program in `fw/`. Longer term, both implementations should come from one semantics table.

## 5. Interface and deliverables

- `python -m shruti prove fw/uart_tx.s` finds `fw/uart_tx.contract.yaml` beside the program. It prints `PROVED (n queries, t s)` or `COUNTEREXAMPLE` with the ISS trace, and exits 0 or 1. A small `shruti/` package provides the CLI, so `shruti load` and `shruti teach` can join it later.
- Tests:
  - The UART TX contract is proved for B in 8..65,535, and the proof time is reported.
  - The prover also reports the smallest B for which the contract holds (expected 4, matching the ISS measurement), by querying with the minimum relaxed.
  - Broken variants each yield an ISS-confirmed counterexample: a missing stop-bit ADDT, `ADDT 1, now` (late start), `ADDT P/2` in the data loop, and MSB-first order.
- Out of v0: UART RX, SPI and I2C contracts (the 29 Nov milestone), symbolic input edges for reactive programs, and a formal link to the RTL.

## Questions for approval

1. Is YAML with a small expression language the right contract format? The alternative is Python contract objects, which are more flexible but harder to read or generate.
2. Is a bound of 2 frames enough for v0? It covers idle-to-first-byte and back-to-back. A proof for an unbounded number of frames needs an inductive invariant at `top`, the loop head: "T is at least the end of the last stop bit, the line is high, and no slot is pending past T". That is a v1 item.
3. Should the CLI live in a new `shruti/` Python package, as proposed, or as a script under `tools/`?
