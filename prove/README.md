# prove: protocol contracts and the prover

`python -m shruti prove <program.s>` symbolically executes the program with the bit period P, the data bytes and their arrival times left symbolic, and asks Z3 whether the contract beside the program (`<program>.contract.yaml`) can be violated. The design is in `DESIGN.md`.

```
$ python -m shruti prove fw/uart_tx.s --min-P
fw/uart_tx.s against fw/uart_tx.contract.yaml:
PROVED for P in 8..65535, all data bytes and arrival times (0.5 s)
PROVED for any number of frames, P in 8..65535, all data bytes and arrival times (0.2 s): base case and induction step hold
smallest P for which the contract holds: 3
```

- **Any number of frames:** a contract can name a loop head and an invariant that holds there (`induction:` in `fw/uart_tx.contract.yaml`). The prover then checks two things.
  - **Base case:** from reset, the first frame is correct and the loop head is reached in a state satisfying the invariant.
  - **Induction step:** from any state satisfying the invariant, one more byte gives a correct frame, leaves the previous stop bit alone, and returns to the loop head satisfying the invariant again.
  - Together these cover every frame of an unbounded stream.
  - Each check first confirms that its assumptions are satisfiable, so a contradictory invariant cannot prove anything.

- **Counterexamples:** the prover re-runs every counterexample on the concrete ISS and prints the ISS's own pin trace, the violated obligation and the first mismatching cycle. A counterexample never depends on trusting the symbolic executor.
- **Obligations:** every contract requires LATE never set, and the executor also checks that T stays within 2^23 cycles of the counter, so unbounded time arithmetic matches the 24-bit hardware.
- **Receivers and SPI:** `fw/uart_rx.contract.yaml` (kind `receiver`) proves the UART receiver against a symbolic 8N1 peer with edge jitter up to P/16, for P = 16..65,535, in about 0.4 s; it also holds from P = 9, and fails at P = 8, matching the ISS limit. `fw/spi_master.contract.yaml` (kind `spi_master`) proves the SPI master's timing and data in both directions against a slave that answers 1 or 2 cycles after each edge, for P = 5..65,535, in about 8 s. MISO samples are left symbolic during execution and tied to a slave model built from the program's own CS and SCK events.
- **Inputs and branches:** the executor forks on branches that depend on symbolic inputs (WAIT, JMP on a pin), keeps only paths whose condition is satisfiable, and can take input waveforms with symbolic edge times and levels. `run()` handles single-path programs; `paths()` returns every feasible path.
- **Trust:** `prove/tests` checks the symbolic executor against the ISS on the UART program and five broken variants with random concrete values.

Still to come: the I2C master (its waits depend on the slave stretching the clock the program itself releases) and the SPI slave.

Tests: `python -m pytest prove`.
