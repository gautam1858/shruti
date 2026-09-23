"""isa.yaml is internally consistent, and decode/encode agree with it."""

import itertools

import pytest

from iss.isa import DecodeError, load_isa

ISA = load_isa()


def test_version():
    assert ISA.version == "1.1"


def test_opcodes_unique_and_halt_is_zero():
    ops = [d.opcode for d in ISA.by_name.values()]
    assert len(ops) == len(set(ops))
    assert ISA.by_opcode[0].name == "HALT"
    assert all(0 <= o < 16 for o in ops)


@pytest.mark.parametrize("name", sorted(ISA.by_name))
def test_fields_tile_the_operand_bits(name):
    d = ISA.by_name[name]
    groups = [list(d.fields)] if not d.variants else [["target"] + v for v in d.variants.values()]
    for group in groups:
        covered = []
        for fname in group:
            f = d.fields[fname]
            assert 0 <= f.lsb <= f.msb <= 11, f"{name}.{fname} outside bits 11..0"
            covered += range(f.lsb, f.msb + 1)
        assert sorted(covered) == list(range(12)), f"{name} {group}: bits not covered exactly once"


def test_enums_fit_their_fields():
    for d in ISA.by_name.values():
        for f in d.fields.values():
            for v in (f.enum or {}).values():
                assert 0 <= v < (1 << f.width)
        for v in (d.flags or {}).values():
            assert 0 <= v < (1 << d.fields["pin"].width)


def test_field_widths_match_decisions():
    w = {n: {f.name: f.width for f in d.fields.values()} for n, d in ISA.by_name.items()}
    assert w["OUT"]["d"] == 6 and w["IN"]["d"] == 6
    assert (w["JMP"]["cond"], w["JMP"]["pin"], w["JMP"]["target"]) == (3, 4, 5)
    assert w["ADDT"]["d"] == 11
    assert w["WAIT"]["pin"] == 4 and w["WAIT"]["mode"] == 3 and w["WAIT"]["timeout"] == 5


def test_every_word_decodes_nonstrict():
    for word in range(1 << 16):
        ins = ISA.decode(word, strict=False)
        assert ins.name in ISA.by_name


def test_strict_decode_rejects_reserved_bits():
    with pytest.raises(DecodeError):
        ISA.decode(0x0001)                     # HALT with a reserved bit set
    assert ISA.decode(0x0001, strict=False).name == "HALT"


def test_undefined_opcodes_decode_as_halt():
    for op in range(12, 16):
        ins = ISA.decode(op << 12)
        assert ins.name == "HALT" and ins.ops["undefined_opcode"] == op


def test_encode_decode_round_trip_all_forms():
    """Every legal combination of small field values survives encode -> decode."""
    for d in ISA.by_name.values():
        groups = [(None, list(d.fields))] if not d.variants else [
            (t, ["target"] + fs) for t, fs in d.variants.items()]
        for tname, names in groups:
            names = [n for n in names if n != "rsv"]
            choices = []
            for n in names:
                f = d.fields[n]
                if n == "target" and tname is not None:
                    choices.append([f.enum[tname]])
                else:
                    top = (1 << f.width) - 1
                    choices.append(sorted({0, 1, top}))
            for combo in itertools.product(*choices):
                ops = dict(zip(names, combo))
                word = ISA.encode(d.name, **ops)
                back = ISA.decode(word)
                assert back.name == d.name
                for k, v in ops.items():
                    assert back.ops[k] == v
