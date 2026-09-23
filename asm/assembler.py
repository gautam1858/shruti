"""Assembler and disassembler for the Shruti Event Machine.

Syntax follows the ISA table in docs/architecture-spec.md; encodings come from isa/isa.yaml
through iss.isa. One instruction per line, `;` or `#` starts a comment, `name:` defines a
label (optionally followed by an instruction on the same line). Mnemonics and keywords are
case-insensitive; labels and aliases are case-sensitive.

    HALT
    WAIT  pin, rise|fall|any|high|low [, timeout]   ; timeout = 2^n cycles, n = 1..23
    OUT   pin, 0|1|OSR|~OSR @T[+d]                  ; d = 0..63
    IN    pin @T[+d]  |  IN pin @snap
    ADDT  d [, now]                                 ; d = 0..2047
    ADDT  P[/2|/4|/8] [, now]
    SHIFT out|in, lsb|msb, n [, auto]               ; n = 1..16
    PUSH  [block]          PULL [block]
    SET   pin, 0|1 [, pp|od]  |  SET X|Y|P, imm     ; imm = 0..1023
    JMP   target  |  JMP always|TO|LATE|TXE|!OSRE|X--|Y--, target  |  JMP HIGH|LOW pin, target
    SYNC  set|wait, n

Directives: `.pin name B0` (pin alias), `.equ name value`, `.word value` (raw word).
Pins are B0-B7, M0-M3 or 0-11. Numbers are decimal, 0x hex or 0b binary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from iss.isa import DecodeError, load_isa

ISA = load_isa()

KEYWORDS = {"X", "Y", "P", "T", "OSR", "HIGH", "LOW", "TO", "LATE", "TXE", "NOW", "BLOCK",
            "AUTO", "SNAP", "ALWAYS", "PP", "OD", "LSB", "MSB", "IN", "OUT", "SET", "WAIT"}


@dataclass
class AsmError(Exception):
    errors: List[Tuple[int, str]]            # (line number, message)
    source: str = "<input>"

    def __str__(self) -> str:
        return "\n".join(f"{self.source}:{ln}: {msg}" for ln, msg in self.errors)


class _LineError(Exception):
    pass


_NUM = re.compile(r"^(0x[0-9a-fA-F]+|0b[01]+|[0-9]+)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Assembler:
    def __init__(self) -> None:
        self.pins: Dict[str, int] = {}
        self.equs: Dict[str, int] = {}
        self.labels: Dict[str, int] = {}

    # -- operands -------------------------------------------------------------------

    def number(self, tok: str, what: str, lo: int, hi: int) -> int:
        tok = tok.strip()
        if _NUM.match(tok):
            v = int(tok, 0)
        elif tok in self.equs:
            v = self.equs[tok]
        else:
            raise _LineError(f"{what}: expected a number, got {tok!r}")
        if not lo <= v <= hi:
            raise _LineError(f"{what} must be {lo}..{hi}, got {v}")
        return v

    def pin(self, tok: str, drivable: bool = False) -> int:
        tok = tok.strip()
        up = tok.upper()
        if tok in self.pins:
            p = self.pins[tok]
        elif up in ISA.pin_names:
            p = ISA.pin_names[up]
        elif _NUM.match(tok):
            p = int(tok, 0)
            if not 0 <= p < ISA.pins:
                raise _LineError(f"pin must be 0..{ISA.pins - 1}, got {p}")
        else:
            raise _LineError(f"unknown pin {tok!r} (use B0-B7, M0-M3 or a .pin alias)")
        if drivable and p >= ISA.output_pins:
            raise _LineError(f"pin {tok} is input-only and cannot be driven")
        return p

    def target(self, tok: str, pass2: bool) -> int:
        tok = tok.strip()
        if _NUM.match(tok):
            return self.number(tok, "jump target", 0, ISA.program_words - 1)
        if not _IDENT.match(tok):
            raise _LineError(f"bad jump target {tok!r}")
        if not pass2:
            return 0
        if tok not in self.labels:
            raise _LineError(f"undefined label {tok!r}")
        return self.labels[tok]

    @staticmethod
    def expect(ops: List[str], lo: int, hi: int, mnem: str, form: str) -> None:
        if not lo <= len(ops) <= hi:
            raise _LineError(f"{mnem} takes {form}")

    # -- one instruction ----------------------------------------------------------

    def encode(self, mnem: str, rest: str, pass2: bool) -> int:
        m = mnem.upper()
        ops = [o.strip() for o in rest.split(",")] if rest.strip() else []
        E = ISA.encode

        if m == "HALT":
            self.expect(ops, 0, 0, m, "no operands")
            return E("HALT")

        if m == "WAIT":
            self.expect(ops, 2, 3, m, "pin, rise|fall|any|high|low [, timeout]")
            mode = ops[1].lower()
            modes = ISA.by_name["WAIT"].fields["mode"].enum
            if mode not in modes:
                raise _LineError(f"WAIT mode must be one of {', '.join(modes)}, got {ops[1]!r}")
            n = 0
            if len(ops) == 3:
                t = ops[2].replace(" ", "")
                if t.startswith("2^"):
                    n = self.number(t[2:], "WAIT timeout exponent", 1, 23)
                else:
                    cycles = self.number(t, "WAIT timeout", 2, 1 << 23)
                    if cycles & (cycles - 1):
                        raise _LineError(f"WAIT timeout must be a power of two (2..2^23), got {cycles}")
                    n = cycles.bit_length() - 1
            return E("WAIT", pin=self.pin(ops[0]), mode=mode, timeout=n)

        if m == "OUT":
            self.expect(ops, 2, 2, m, "pin, 0|1|OSR|~OSR @T+d")
            mt = re.match(r"^(0|1|OSR|~OSR)\s*@\s*T\s*(?:\+\s*(\S+))?$", ops[1], re.I)
            if not mt:
                raise _LineError(f"OUT value must look like '1 @T+4' or 'OSR @T', got {ops[1]!r}")
            d = self.number(mt.group(2), "OUT delay d", 0, 63) if mt.group(2) else 0
            return E("OUT", pin=self.pin(ops[0], drivable=True), value=mt.group(1).upper(), d=d)

        if m == "IN":
            self.expect(ops, 1, 1, m, "pin @T+d or pin @snap")
            mt = re.match(r"^(\S+)\s*@\s*(?:(snap)|T\s*(?:\+\s*(\S+))?)$", ops[0], re.I)
            if not mt:
                raise _LineError(f"IN takes 'pin @T+d' or 'pin @snap', got {ops[0]!r}")
            pin = self.pin(mt.group(1))
            if mt.group(2):
                return E("IN", pin=pin, snap=1, d=0)
            d = self.number(mt.group(3), "IN delay d", 0, 63) if mt.group(3) else 0
            return E("IN", pin=pin, snap=0, d=d)

        if m == "ADDT":
            self.expect(ops, 1, 2, m, "d [, now] or P[/2|/4|/8] [, now]")
            now = 0
            if len(ops) == 2:
                if ops[1].lower() != "now":
                    raise _LineError(f"ADDT's second operand must be 'now', got {ops[1]!r}")
                now = 1
            mp = re.match(r"^P\s*(?:/\s*(2|4|8))?$", ops[0], re.I)
            if mp:
                shift = {None: 0, "2": 1, "4": 2, "8": 3}[mp.group(1)]
                return E("ADDTP", now=now, shift=shift)
            return E("ADDT", now=now, d=self.number(ops[0], "ADDT d", 0, 2047))

        if m == "SHIFT":
            self.expect(ops, 3, 4, m, "out|in, lsb|msb, n [, auto]")
            d, o = ops[0].lower(), ops[1].lower()
            if d not in ("out", "in"):
                raise _LineError(f"SHIFT direction must be out or in, got {ops[0]!r}")
            if o not in ("lsb", "msb"):
                raise _LineError(f"SHIFT order must be lsb or msb, got {ops[1]!r}")
            n = self.number(ops[2], "SHIFT word length n", 1, 16)
            auto = 0
            if len(ops) == 4:
                if ops[3].lower() != "auto":
                    raise _LineError(f"SHIFT's fourth operand must be 'auto', got {ops[3]!r}")
                auto = 1
            return E("SHIFT", dir=d, msb=o, auto=auto, n=n - 1)

        if m in ("PUSH", "PULL"):
            self.expect(ops, 0, 1, m, "no operand or 'block'")
            if ops and ops[0].lower() != "block":
                raise _LineError(f"{m} takes no operand or 'block', got {ops[0]!r}")
            return E(m, block=1 if ops else 0)

        if m == "SET":
            self.expect(ops, 2, 3, m, "pin, 0|1 [, pp|od] or X|Y|P, imm")
            if ops[0].upper() in ("X", "Y", "P"):
                if len(ops) != 2:
                    raise _LineError("SET X|Y|P takes one value")
                return E("SET", target=ops[0].upper(),
                         imm=self.number(ops[1], f"SET {ops[0].upper()} value", 0, 1023))
            level = self.number(ops[1], "SET pin level", 0, 1)
            od = 0
            if len(ops) == 3:
                mode = ops[2].lower()
                if mode not in ("pp", "od"):
                    raise _LineError(f"SET pin mode must be pp or od, got {ops[2]!r}")
                od = 1 if mode == "od" else 0
            return E("SET", target="pin", pin=self.pin(ops[0], drivable=True), level=level, od=od)

        if m == "JMP":
            self.expect(ops, 1, 2, m, "target, cond, target, or HIGH|LOW pin, target")
            if len(ops) == 1:
                return E("JMP", cond="flag", pin="always", target=self.target(ops[0], pass2))
            c = ops[0].replace(" ", "")
            cu = c.upper()
            tgt = self.target(ops[1], pass2)
            flags = {k.upper(): k for k in ISA.by_name["JMP"].flags}
            if cu in flags:
                return E("JMP", cond="flag", pin=flags[cu], target=tgt)
            if cu in ("X--", "X--!=0", "Y--", "Y--!=0"):
                return E("JMP", cond=cu[:3], pin=0, target=tgt)
            mh = re.match(r"^(HIGH|LOW)\s+(\S+)$", ops[0].strip(), re.I)
            if mh:
                return E("JMP", cond=mh.group(1).lower(), pin=self.pin(mh.group(2)), target=tgt)
            raise _LineError(f"unknown JMP condition {ops[0]!r} (always, TO, LATE, TXE, "
                             f"!OSRE, X--, Y--, HIGH pin, LOW pin)")

        if m == "SYNC":
            self.expect(ops, 2, 2, m, "set|wait, n")
            op = ops[0].lower()
            if op not in ("set", "wait"):
                raise _LineError(f"SYNC operation must be set or wait, got {ops[0]!r}")
            return E("SYNC", op=op, flag=self.number(ops[1], "SYNC flag", 0, ISA.sync_flags - 1))

        raise _LineError(f"unknown mnemonic {mnem!r}")

    # -- whole program --------------------------------------------------------------

    def assemble(self, text: str, source: str = "<input>") -> List[int]:
        lines = text.splitlines()
        errors: List[Tuple[int, str]] = []
        items: List[Tuple[int, str, str]] = []     # (line number, mnemonic, operands)
        pc = 0
        for ln, raw in enumerate(lines, 1):
            line = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
            try:
                while True:
                    ml = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
                    if not ml:
                        break
                    name, line = ml.group(1), ml.group(2).strip()
                    if name in self.labels:
                        raise _LineError(f"duplicate label {name!r}")
                    if name.upper() in KEYWORDS or name.upper() in ISA.pin_names:
                        raise _LineError(f"label {name!r} is a reserved word")
                    self.labels[name] = pc
                if not line:
                    continue
                parts = line.split(None, 1)
                mnem, rest = parts[0], parts[1] if len(parts) > 1 else ""
                if mnem.lower() == ".pin":
                    a = rest.split()
                    if len(a) != 2 or not _IDENT.match(a[0]):
                        raise _LineError(".pin takes a name and a pin, e.g. '.pin tx B0'")
                    if a[0].upper() in KEYWORDS or a[0].upper() in ISA.pin_names:
                        raise _LineError(f"alias {a[0]!r} is a reserved word")
                    self.pins[a[0]] = self.pin(a[1])
                    continue
                if mnem.lower() == ".equ":
                    a = rest.split()
                    if len(a) != 2 or not _IDENT.match(a[0]):
                        raise _LineError(".equ takes a name and a value")
                    self.equs[a[0]] = self.number(a[1], ".equ value", 0, 0xFFFF)
                    continue
                items.append((ln, mnem, rest))
                pc += 1
            except _LineError as e:
                errors.append((ln, str(e)))
        if pc > ISA.program_words:
            errors.append((items[ISA.program_words][0],
                           f"program is {pc} words; an Event Machine holds {ISA.program_words}"))
        words: List[int] = []
        for ln, mnem, rest in items:
            try:
                if mnem.lower() == ".word":
                    words.append(self.number(rest, ".word", 0, 0xFFFF))
                else:
                    words.append(self.encode(mnem, rest, pass2=True))
            except _LineError as e:
                errors.append((ln, str(e)))
        if errors:
            raise AsmError(sorted(errors), source)
        return words


def assemble(text: str, source: str = "<input>") -> List[int]:
    return Assembler().assemble(text, source)


# -- disassembler ------------------------------------------------------------------

_PIN_NAME = {v: k for k, v in ISA.pin_names.items()}


def disassemble_word(word: int, labels: Optional[Dict[int, str]] = None) -> str:
    """Canonical text for one word, or `.word 0x....` if the assembler cannot express it."""
    raw = f".word 0x{word:04X}"
    try:
        ins = ISA.decode(word, strict=True)
    except DecodeError:
        return raw
    o = ins.ops
    n = ins.name
    tgt = (lambda t: labels.get(t, str(t)) if labels else str(t))

    def pin(p, drivable=False):
        if p >= ISA.pins or (drivable and p >= ISA.output_pins):
            raise ValueError
        return _PIN_NAME[p]

    try:
        if n == "HALT":
            return raw if "undefined_opcode" in o else "HALT"
        if n == "WAIT":
            mode = ISA.enum_name("WAIT", "mode", o["mode"])
            if mode is None or o["timeout"] > 23:
                return raw
            s = f"WAIT {pin(o['pin'])}, {mode}"
            return s + (f", {1 << o['timeout']}" if o["timeout"] else "")
        if n == "OUT":
            v = ISA.enum_name("OUT", "value", o["value"])
            return f"OUT {pin(o['pin'], True)}, {v} @T+{o['d']}"
        if n == "IN":
            if o["snap"]:
                return raw if o["d"] else f"IN {pin(o['pin'])} @snap"
            return f"IN {pin(o['pin'])} @T+{o['d']}"
        if n == "ADDT":
            return f"ADDT {o['d']}" + (", now" if o["now"] else "")
        if n == "ADDTP":
            return "ADDT P" + ["", "/2", "/4", "/8"][o["shift"]] + (", now" if o["now"] else "")
        if n == "SHIFT":
            d = ISA.enum_name("SHIFT", "dir", o["dir"])
            m = ISA.enum_name("SHIFT", "msb", o["msb"])
            return f"SHIFT {d}, {m}, {o['n'] + 1}" + (", auto" if o["auto"] else "")
        if n in ("PUSH", "PULL"):
            return n + (" block" if o["block"] else "")
        if n == "SET":
            t = ISA.enum_name("SET", "target", o["target"])
            if t == "pin":
                return f"SET {pin(o['pin'], True)}, {o['level']}" + (", od" if o["od"] else "")
            return f"SET {t}, {o['imm']}"
        if n == "JMP":
            cond = ISA.enum_name("JMP", "cond", o["cond"])
            t = tgt(o["target"])
            if cond == "flag":
                flag = next((k for k, v in ISA.by_name["JMP"].flags.items() if v == o["pin"]), None)
                if flag is None:
                    return raw
                return f"JMP {t}" if flag == "always" else f"JMP {flag}, {t}"
            if cond in ("X--", "Y--"):
                return raw if o["pin"] else f"JMP {cond}, {t}"
            if cond in ("high", "low"):
                return f"JMP {cond.upper()} {pin(o['pin'])}, {t}"
            return raw
        if n == "SYNC":
            return f"SYNC {ISA.enum_name('SYNC', 'op', o['op'])}, {o['flag']}"
    except ValueError:
        return raw
    return raw


def disassemble(words: Sequence[int], labels: bool = True) -> str:
    """Disassemble a program. With labels=True, jump targets become L<n> labels."""
    lab: Dict[int, str] = {}
    if labels:
        for w in words:
            text = disassemble_word(w)
            if text.startswith("JMP"):
                t = ISA.decode(w).ops["target"]
                if t < len(words):            # targets past the end stay numeric
                    lab[t] = ""
        lab = {t: f"L{t}" for t in sorted(lab)}
    out = []
    for i, w in enumerate(words):
        prefix = f"{lab[i]}:" if i in lab else ""
        out.append(f"{prefix:<6}{disassemble_word(w, lab or None)}")
    return "\n".join(out) + "\n"
