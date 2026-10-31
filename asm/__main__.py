"""Command line: python -m asm prog.s [-o prog.hex] [-D NAME=VALUE ...]  |  python -m asm -d prog.hex"""

import argparse
import sys

from .assembler import AsmError, assemble, disassemble


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m asm", description=__doc__)
    ap.add_argument("file")
    ap.add_argument("-o", "--output", help="write hex words here (default: stdout)")
    ap.add_argument("-d", "--disassemble", action="store_true", help="input is hex words")
    ap.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME=VALUE",
                    help="override an .equ, e.g. -D CPOL=1 (repeatable)")
    a = ap.parse_args(argv)
    text = open(a.file).read()
    if a.disassemble:
        words = [int(t, 16) for t in text.split()]
        out = disassemble(words)
    else:
        try:
            defs = {}
            for d in a.defines:
                name, _, value = d.partition("=")
                defs[name] = int(value, 0)
            words = assemble(text, a.file, defs)
        except AsmError as e:
            print(e, file=sys.stderr)
            return 1
        out = "".join(f"{w:04X}\n" for w in words)
    if a.output:
        open(a.output, "w").write(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
