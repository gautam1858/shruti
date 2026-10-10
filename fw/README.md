# fw: firmware library

Event Machine programs, each tested on the ISS against a Python model of the peer device (`fw/peers.py`). Contracts and proofs arrive with the prover.

| Program | Words | Host setup | Peer model in the tests | Measured limit at 50 MHz |
| --- | --- | --- | --- | --- |
| `uart_tx.s` | 12 | P = bit period | 8N1 receiver decoding the pin trace | B >= 4 |
| `uart_rx.s` | 17 | P = bit period | 8N1 transmitter, ±3% clock error, framing errors, glitches | B >= 9 |
| `spi_master.s` | 19 | P = half SCK period; mode at assembly (`-D CPOL=1 -D CPHA=1`) | slave in each of modes 0-3, full duplex | P >= 5 (5 MHz SCK) in every mode |
| `spi_slave.s` | 18 (CPHA 0), 15 (CPHA 1) | none (CS, SCK from the master); mode at assembly | master model in each of modes 0-3; EM0 master paired with EM1 slave in each mode | SCK phase >= 4; CS to first SCK edge >= 5 (CPHA 0) or >= 3 (CPHA 1) |
| `i2c_master.s` | 32 | P = SCL low time; speed grade at assembly (`-D FAST=1`: SCL high for P/2); CFG = MSB-first, 8 bits | slave with ACK/NACK and clock stretching | 100.0 kHz at P = 249; 400.0 kHz at P = 82 with FAST = 1 (P >= 65 for fast-mode tLOW) |
| `i2c_target.s` | 32 | none; the host answers each received byte, address included, with 0x00 (ACK) or 0xFF (NACK), and SCL is stretched until it does | stretching-aware master model with any data hold time, NACKs and repeated STARTs; EM0 master paired with EM1 target at 100 and 400 kHz | SCL high >= 4 cycles, low >= 11 (about 3.3 MHz); writes only |

```python
import fw
words = fw.load("uart_tx")        # assembles fw/uart_tx.s
```

Tests: `python -m pytest fw`.
