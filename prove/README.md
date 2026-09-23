# prove: protocol contracts and the prover

`python -m shruti prove <program.s>` symbolically executes the program with the bit period P, the data bytes and their arrival times left symbolic, and asks Z3 whether the contract beside the program (`<program>.contract.yaml`) can be violated. The design is in `DESIGN.md`.

```
$ python -m shruti prove fw/uart_tx.s --min-P
fw/uart_tx.s against fw/uart_tx.contract.yaml:
PROVED for P in 8..65535, all data bytes and arrival times (0.5 s)
smallest P for which the contract holds: 3
```

- **Counterexamples:** the prover re-runs every counterexample on the concrete ISS and prints the ISS's own pin trace, the violated obligation and the first mismatching cycle. A counterexample never depends on trusting the symbolic executor.
- **Obligations:** every contract requires LATE never set, and the executor also checks that T stays within 2^23 cycles of the counter, so unbounded time arithmetic matches the 24-bit hardware.
- **Scope of v0:** programs whose control flow does not depend on symbolic values, which here means transmitters such as `uart_tx.s`. WAIT, IN and JMP on pins, TO or TXE raise `Unsupported`; they come with reactive contracts.
- **Trust:** `prove/tests` checks the symbolic executor against the ISS on the UART program and five broken variants with random concrete values.

Tests: `python -m pytest prove`.
