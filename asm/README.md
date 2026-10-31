# asm: assembler and disassembler

Converts the ISA's text form (see `docs/architecture-spec.md`, "Event-native datapath and ISA") to 16-bit words and back. Encodings come from `isa/isa.yaml` through `iss/isa.py`. The syntax reference is the docstring at the top of `asm/assembler.py`.

```
python -m asm prog.s -o prog.hex     # assemble; errors are reported as file:line: message
python -m asm -d prog.hex            # disassemble, with L<n> labels for jump targets
python -m asm fw/spi_master.s -D CPOL=1 -D CPHA=1   # override .equ values (SPI mode 3)
```

One source can give several variants of a program: `.if name` / `.if !name` / `.else` / `.endif` select lines on an `.equ` value, `.define name text` swaps an operand token (for example `.define LEAD rise`), and `-D` (or `defines=` in Python, or keyword arguments to `fw.load`) overrides an `.equ`. A define that names no `.equ` in the program is an error.

```python
from asm import assemble, disassemble
words = assemble(open("fw/uart_tx.s").read())
```

Round trips are exhaustive: all 65,536 words give `assemble(disassemble(w)) == w`. The 12,609 legal encodings disassemble to instructions; everything else becomes `.word`. Tests: `python -m pytest asm`.
