# fw: firmware library

Event Machine programs, each tested on the ISS against a Python model of the peer device (`fw/peers.py`). Contracts and proofs arrive with the prover.

| Program | Words | Host setup | Peer model in the tests | Measured limit at 50 MHz |
| --- | --- | --- | --- | --- |
| `uart_tx.s` | 12 | P = bit period | 8N1 receiver decoding the pin trace | B >= 4 |
| `uart_rx.s` | 17 | P = bit period | 8N1 transmitter, ±3% clock error, framing errors, glitches | B >= 9 |
| `spi_master.s` | 19 | P = half SCK period | mode-0 slave, full duplex | P >= 5 (5 MHz SCK) |
| `i2c_master.s` | 32 | P = half SCL period; CFG = MSB-first, 8 bits | slave with ACK/NACK and clock stretching | P >= 65 for fast-mode tLOW |

```python
import fw
words = fw.load("uart_tx")        # assembles fw/uart_tx.s
```

Tests: `python -m pytest fw`.
