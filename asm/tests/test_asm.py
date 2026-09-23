"""Assembler and disassembler: every form, round trips, and error messages."""

import pytest

from asm import AsmError, assemble, disassemble, disassemble_word
from iss.isa import load_isa

ISA = load_isa()


def one(line):
    words = assemble(line)
    assert len(words) == 1
    return words[0]


# Every instruction form in the syntax, with the fields it must produce.
FORMS = [
    ("HALT", "HALT", {}),
    ("WAIT B3, rise", "WAIT", dict(pin=3, mode=0, timeout=0)),
    ("WAIT M0, fall, 1024", "WAIT", dict(pin=8, mode=1, timeout=10)),
    ("WAIT m3, any, 2^23", "WAIT", dict(pin=11, mode=2, timeout=23)),
    ("WAIT B0, high", "WAIT", dict(pin=0, mode=3, timeout=0)),
    ("WAIT 7, low, 2", "WAIT", dict(pin=7, mode=4, timeout=1)),
    ("OUT B0, 0 @T+0", "OUT", dict(pin=0, value=0, d=0)),
    ("OUT B7, 1 @T+63", "OUT", dict(pin=7, value=1, d=63)),
    ("OUT B1, OSR @T", "OUT", dict(pin=1, value=2, d=0)),
    ("OUT B2, ~osr @ T + 5", "OUT", dict(pin=2, value=3, d=5)),
    ("IN M1 @T+3", "IN", dict(pin=9, snap=0, d=3)),
    ("IN B4 @T", "IN", dict(pin=4, snap=0, d=0)),
    ("IN B4 @snap", "IN", dict(pin=4, snap=1, d=0)),
    ("ADDT 2047", "ADDT", dict(now=0, d=2047)),
    ("ADDT 2, now", "ADDT", dict(now=1, d=2)),
    ("ADDT P", "ADDTP", dict(now=0, shift=0)),
    ("ADDT P/2", "ADDTP", dict(now=0, shift=1)),
    ("ADDT P/4, now", "ADDTP", dict(now=1, shift=2)),
    ("ADDT P/8", "ADDTP", dict(now=0, shift=3)),
    ("SHIFT out, lsb, 8", "SHIFT", dict(dir=0, msb=0, auto=0, n=7)),
    ("SHIFT in, msb, 16, auto", "SHIFT", dict(dir=1, msb=1, auto=1, n=15)),
    ("SHIFT out, msb, 1", "SHIFT", dict(dir=0, msb=1, auto=0, n=0)),
    ("PUSH", "PUSH", dict(block=0)),
    ("PUSH block", "PUSH", dict(block=1)),
    ("PULL", "PULL", dict(block=0)),
    ("PULL block", "PULL", dict(block=1)),
    ("SET B5, 1", "SET", dict(target=0, pin=5, level=1, od=0)),
    ("SET B5, 0, od", "SET", dict(target=0, pin=5, level=0, od=1)),
    ("SET B5, 1, pp", "SET", dict(target=0, pin=5, level=1, od=0)),
    ("SET X, 1023", "SET", dict(target=1, imm=1023)),
    ("SET Y, 0x10", "SET", dict(target=2, imm=16)),
    ("SET P, 0b101", "SET", dict(target=3, imm=5)),
    ("JMP 31", "JMP", dict(cond=0, pin=0, target=31)),
    ("JMP always, 3", "JMP", dict(cond=0, pin=0, target=3)),
    ("JMP TO, 4", "JMP", dict(cond=0, pin=1, target=4)),
    ("JMP LATE, 4", "JMP", dict(cond=0, pin=2, target=4)),
    ("JMP TXE, 4", "JMP", dict(cond=0, pin=3, target=4)),
    ("JMP !OSRE, 4", "JMP", dict(cond=0, pin=4, target=4)),
    ("JMP X--, 6", "JMP", dict(cond=1, pin=0, target=6)),
    ("JMP X--!=0, 6", "JMP", dict(cond=1, pin=0, target=6)),
    ("JMP Y--, 6", "JMP", dict(cond=2, pin=0, target=6)),
    ("JMP HIGH M2, 7", "JMP", dict(cond=3, pin=10, target=7)),
    ("JMP low B1, 7", "JMP", dict(cond=4, pin=1, target=7)),
    ("SYNC set, 0", "SYNC", dict(op=0, flag=0)),
    ("SYNC wait, 3", "SYNC", dict(op=1, flag=3)),
]


@pytest.mark.parametrize("line,name,fields", FORMS, ids=[f[0] for f in FORMS])
def test_every_form_encodes_as_specified(line, name, fields):
    word = one(line)
    ins = ISA.decode(word)
    assert ins.name == name
    for k, v in fields.items():
        assert ins.ops[k] == v, k


@pytest.mark.parametrize("line", [f[0] for f in FORMS])
def test_form_round_trip(line):
    w1 = one(line)
    text = disassemble_word(w1)
    w2 = one(text)
    assert w1 == w2
    assert disassemble_word(w2) == text


def test_every_16_bit_word_round_trips():
    """assemble(disassemble(w)) == w for all 65,536 words; illegal ones become .word."""
    expressible = 0
    for w in range(1 << 16):
        text = disassemble_word(w)
        assert one(text) == w, (hex(w), text)
        expressible += not text.startswith(".word")
    # Legal encodings, counted independently from the field ranges:
    legal = (1                        # HALT
             + 12 * 5 * 24            # WAIT: pin, mode, timeout 0..23
             + 8 * 4 * 64             # OUT
             + 12 * 64 + 12           # IN live, IN @snap
             + 2 * 2048 + 2 * 4       # ADDT, ADDT P
             + 2 * 2 * 2 * 16         # SHIFT
             + 2 + 2                  # PUSH, PULL
             + 8 * 2 * 2 + 3 * 1024   # SET pin, SET X|Y|P
             + (5 + 2 + 2 * 12) * 32  # JMP flag, X--/Y--, HIGH/LOW pin
             + 2 * 4)                 # SYNC
    assert expressible == legal == 12_609


def test_program_round_trip_with_labels():
    src = """
    .pin tx B0
    .equ HALFBIT 12
            SET   tx, 1
            SHIFT out, lsb, 8
    top:    PULL  block
            ADDT  2, now
            OUT   tx, 0 @T+0
    bit:    ADDT  P
            OUT   tx, OSR @T+0
            JMP   !OSRE, bit
            ADDT  HALFBIT
            OUT   tx, 1 @T+0
            ADDT  P
            JMP   top
    """
    w1 = assemble(src)
    text = disassemble(w1)
    assert assemble(text) == w1
    assert "L2:" in text and "JMP !OSRE, L5" in text


def test_label_on_its_own_line_and_forward_reference():
    w = assemble("  JMP end\nloop:\n  JMP loop\nend: HALT\n")
    assert [ISA.decode(x).ops.get("target") for x in w[:2]] == [2, 1]


def test_comments_and_blank_lines():
    assert assemble("; header\n\n  HALT  # trailing\n") == [0]


# -- errors carry line numbers and say what is wrong ------------------------------------

@pytest.mark.parametrize("src,line,fragment", [
    ("HALT\nOUTT B0, 1 @T", 2, "unknown mnemonic 'OUTT'"),
    ("OUT B0, 1 @T+64", 1, "OUT delay d must be 0..63, got 64"),
    ("OUT M0, 1 @T", 1, "input-only"),
    ("SET M1, 1", 1, "input-only"),
    ("IN B12 @T", 1, "unknown pin 'B12'"),
    ("WAIT B0, up", 1, "WAIT mode must be one of"),
    ("WAIT B0, rise, 1000", 1, "power of two"),
    ("WAIT B0, rise, 2^24", 1, "exponent must be 1..23"),
    ("ADDT 2048", 1, "ADDT d must be 0..2047, got 2048"),
    ("ADDT 5, later", 1, "must be 'now'"),
    ("SET X, 1024", 1, "must be 0..1023"),
    ("SET B0, 2", 1, "SET pin level must be 0..1"),
    ("SHIFT out, lsb, 17", 1, "must be 1..16"),
    ("SHIFT sideways, lsb, 8", 1, "direction must be out or in"),
    ("JMP nowhere", 1, "undefined label 'nowhere'"),
    ("JMP 32", 1, "jump target must be 0..31"),
    ("JMP Z--, 0", 1, "unknown JMP condition"),
    ("a: HALT\na: HALT", 2, "duplicate label 'a'"),
    ("SYNC set, 4", 1, "SYNC flag must be 0..3"),
    ("PULL now", 1, "PULL takes no operand or 'block'"),
    ("HALT 3", 1, "HALT takes no operands"),
    (".pin X B0", 1, "reserved word"),
])
def test_errors_have_line_numbers(src, line, fragment):
    with pytest.raises(AsmError) as ei:
        assemble(src, "prog.s")
    lines = [ln for ln, _ in ei.value.errors]
    assert line in lines
    assert fragment in str(ei.value)
    assert f"prog.s:{line}:" in str(ei.value)


def test_all_errors_are_reported_not_just_the_first():
    with pytest.raises(AsmError) as ei:
        assemble("OUTT\nHALT\nADDT 9999\nJMP nope\n")
    assert [ln for ln, _ in ei.value.errors] == [1, 3, 4]


def test_program_too_long():
    with pytest.raises(AsmError) as ei:
        assemble("HALT\n" * 33)
    assert "33 words" in str(ei.value) and ei.value.errors[0][0] == 33


def test_cli_assemble_and_disassemble(tmp_path):
    from asm.__main__ import main
    src = tmp_path / "p.s"
    src.write_text("top: ADDT P\n JMP top\n")
    hexf = tmp_path / "p.hex"
    assert main([str(src), "-o", str(hexf)]) == 0
    assert hexf.read_text() == "5000\nA000\n"
    out = tmp_path / "p.dis"
    assert main(["-d", str(hexf), "-o", str(out)]) == 0
    assert "JMP L0" in out.read_text()
