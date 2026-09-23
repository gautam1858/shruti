# iss: cycle-accurate instruction-set simulator

The golden model. It was written before any RTL, and its decode tables are generated from `isa/isa.yaml` (`iss/isa.py`). It is the reference for differential testing and for `shruti prove`.

What it models:
- one or two Event Machines;
- the free-running 24-bit timestamp counter;
- input synchronisation with a configurable latency (default 2 cycles), edge capture and the edge-latched 12-pin snapshot;
- two scheduler slots per EM;
- 4-deep TX and RX FIFOs;
- the four SYNC flags;
- pin drive in push-pull and open-drain modes.

Timing follows the model in `isa.yaml` exactly. The run loop is event-driven: it skips cycles in which nothing can change. `step_every_cycle=True` is a reference mode, and the tests check that both modes give identical results.

```python
from iss import simulate
res = simulate(program_words, cycles,
               P=5208,                          # bit period register
               stimulus=[(cycle, pin, level)],  # external edges
               host_tx=[(cycle, byte)],         # host writes to the TX FIFO
               initial={pin: level})
res.trace          # [(cycle, pin, level)] pad changes
res.host_rx        # bytes the EM pushed, as drained by the host
res.ems[0]         # final architectural state (T, X, Y, OSR, flags, ...)
```

Peer device models (for firmware tests) subclass `iss.Device` and schedule external pin changes with `sim.set_external`.

Tests: `python -m pytest iss`.
