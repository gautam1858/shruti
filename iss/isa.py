"""Instruction-set tables generated from isa/isa.yaml.

Everything the ISS and the assembler know about encodings comes from this module, and
everything in this module comes from isa.yaml: opcodes, field bit ranges, enumerations,
JMP flag selectors and SET variants. Nothing about the encoding is written by hand here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

ISA_PATH = Path(__file__).resolve().parent.parent / "isa" / "isa.yaml"


class DecodeError(Exception):
    """A word that cannot be decoded in strict mode (nonzero reserved bits)."""


@dataclass(frozen=True)
class Field:
    name: str
    msb: int
    lsb: int
    enum: Optional[Dict[str, int]] = None

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1

    def extract(self, word: int) -> int:
        return (word >> self.lsb) & ((1 << self.width) - 1)

    def insert(self, value: int) -> int:
        if not 0 <= value < (1 << self.width):
            raise ValueError(f"value {value} does not fit field {self.name} ({self.width} bits)")
        return value << self.lsb


@dataclass(frozen=True)
class InstrDef:
    name: str
    opcode: int
    asm: str
    fields: Dict[str, Field]
    variants: Optional[Dict[str, List[str]]] = None   # SET: target enum name -> field names
    flags: Optional[Dict[str, int]] = None            # JMP: flag selector names
    semantics: str = ""

    def active_fields(self, word: int) -> List[Field]:
        """The fields that apply to this word (SET selects a variant by its target)."""
        if not self.variants:
            return list(self.fields.values())
        tfield = self.fields["target"]
        tname = _enum_name(tfield, tfield.extract(word))
        names = ["target"] + self.variants[tname]
        return [self.fields[n] for n in names]


@dataclass(frozen=True)
class Instr:
    """A decoded instruction: its definition and raw field values."""

    name: str
    word: int
    ops: Dict[str, int] = field(default_factory=dict)

    def __getitem__(self, key: str) -> int:
        return self.ops[key]


def _enum_name(f: Field, value: int) -> str:
    assert f.enum is not None
    for k, v in f.enum.items():
        if v == value:
            return k
    raise KeyError(value)


def _parse_field(name: str, spec) -> Field:
    if isinstance(spec, list):
        return Field(name, spec[0], spec[1])
    bits = spec["bits"]
    enum = {str(k): int(v) for k, v in spec.get("enum", {}).items()} or None
    return Field(name, bits[0], bits[1], enum)


class ISA:
    def __init__(self, path: Path = ISA_PATH):
        with open(path) as fh:
            self.raw = yaml.safe_load(fh)
        r = self.raw
        self.version: str = str(r["version"])
        self.word_bits: int = r["word_bits"]
        self.opcode_bits: int = r["opcode_bits"]
        self.program_words: int = r["program_words"]
        self.counter_bits: int = r["counter_bits"]
        self.pins: int = r["pins"]
        self.output_pins: int = r["output_pins"]
        self.fifo_depth: int = r["fifo_depth"]
        self.sync_flags: int = r["sync_flags"]
        self.pin_names: Dict[str, int] = {str(k): int(v) for k, v in r["pin_names"].items()}
        self.operand_bits = self.word_bits - self.opcode_bits
        self.by_name: Dict[str, InstrDef] = {}
        self.by_opcode: Dict[int, InstrDef] = {}
        for ins in r["instructions"]:
            fields = {n: _parse_field(n, s) for n, s in (ins.get("fields") or {}).items()}
            flags = {str(k): int(v) for k, v in ins["flags"].items()} if "flags" in ins else None
            d = InstrDef(
                name=ins["name"],
                opcode=ins["opcode"],
                asm=ins.get("asm", ins["name"]),
                fields=fields,
                variants=ins.get("variants"),
                flags=flags,
                semantics=ins.get("semantics", ""),
            )
            self.by_name[d.name] = d
            self.by_opcode[d.opcode] = d

    # -- decode / encode --------------------------------------------------------------

    def decode(self, word: int, strict: bool = True) -> Instr:
        """Decode a 16-bit word. Undefined opcodes decode as HALT (as in hardware)."""
        if not 0 <= word < (1 << self.word_bits):
            raise ValueError(f"word {word:#x} is not {self.word_bits} bits")
        opcode = word >> self.operand_bits
        d = self.by_opcode.get(opcode)
        if d is None:
            return Instr("HALT", word, {"undefined_opcode": opcode})
        ops = {f.name: f.extract(word) for f in d.active_fields(word)}
        if strict:
            for f in d.active_fields(word):
                if f.name == "rsv" and ops["rsv"] != 0:
                    raise DecodeError(f"{d.name}: nonzero reserved bits in {word:#06x}")
        return Instr(d.name, word, ops)

    def encode(self, name: str, **ops) -> int:
        """Encode an instruction from field values (ints, or enum names as strings)."""
        d = self.by_name[name]
        word = d.opcode << self.operand_bits
        if d.variants:
            tfield = d.fields["target"]
            t = ops.get("target", 0)
            tname = t if isinstance(t, str) else _enum_name(tfield, t)
            allowed = ["target"] + d.variants[tname]
        else:
            allowed = list(d.fields)
        for key, value in ops.items():
            if key not in allowed:
                raise ValueError(f"{name}: no field {key!r} (fields: {', '.join(allowed)})")
            f = d.fields[key]
            if isinstance(value, str):
                if key == "pin" and name == "JMP" and d.flags and value in d.flags:
                    value = d.flags[value]
                elif f.enum and value in f.enum:
                    value = f.enum[value]
                else:
                    raise ValueError(f"{name}: {value!r} is not a valid {key}")
            word |= f.insert(int(value))
        return word

    def enum_value(self, instr: str, fld: str, name: str) -> int:
        f = self.by_name[instr].fields[fld]
        assert f.enum is not None
        return f.enum[name]

    def enum_name(self, instr: str, fld: str, value: int) -> Optional[str]:
        f = self.by_name[instr].fields[fld]
        if not f.enum:
            return None
        for k, v in f.enum.items():
            if v == value:
                return k
        return None


_ISA: Optional[ISA] = None


def load_isa() -> ISA:
    """The ISA loaded from isa/isa.yaml (cached)."""
    global _ISA
    if _ISA is None:
        _ISA = ISA()
    return _ISA


def field_layout(d: InstrDef) -> List[Tuple[str, int, int]]:
    return [(f.name, f.msb, f.lsb) for f in d.fields.values()]
