# asm: assembler and disassembler

Converts the ISA's text form (see `docs/architecture-spec.md`, "Event-native datapath and ISA") to 16-bit words and back. Encodings come from `isa/isa.yaml` through `iss/isa.py`. The syntax reference is the docstring at the top of `asm/assembler.py`.

```
python -m asm prog.s -o prog.hex     # assemble; errors are reported as file:line: message
python -m asm -d prog.hex            # disassemble, with L<n> labels for jump targets
```

```python
from asm import assemble, disassemble
words = assemble(open("fw/uart_tx.s").read())
```

Round trips are exhaustive: all 65,536 words give `assemble(disassemble(w)) == w`. The 12,609 legal encodings disassemble to instructions; everything else becomes `.word`. Tests: `python -m pytest asm`.
