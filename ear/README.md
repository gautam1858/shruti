# ear: the on-die protocol identifier

| File | What it is |
| --- | --- |
| `SPEC.md` | The bit-exact definition the RTL must match: input path, windows, the 72 features, the ternary classifier, the weight chain |
| `inpath.py` | Synchroniser, glitch filter and edge detect, register by register as in `src/project.v` (checked against the RTL by `test/test.py`) |
| `features.py` | The feature extractor |
| `mlp.py` | The ternary classifier and the 1,344-bit weight chain |
| `protosim.py` | Protocol simulator: edge streams for UART, SPI, I2C, low-speed USB, CAN, JTAG/SWD, PS/2 and Manchester, each checked by decoding it back |
| `dataset.py`, `train.py` | Training data from the simulator, quantisation-aware training, export and evaluation of the exact integer model |
| `weights/default.*` | The current weights (`.hex`, loaded over SPI) and configuration (`.json`) |
| `REPORT.md` | Accuracy, confusion matrix and out-of-distribution rates of the current weights |

```
python -m shruti train                     # regenerate weights/default.* and REPORT.md
python -m pytest ear
```

Status: the numbers in `REPORT.md` come from simulated buses only; captures of real devices on the FPGA come next. The out-of-distribution flag, a floor on the best logit, flagged almost none of the held-out protocols in the first check. A better unknown-traffic test is open work.
