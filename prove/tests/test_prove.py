"""The prover: the UART TX proof, ISS-confirmed counterexamples, and the symbolic executor
checked against the ISS."""

import random
from pathlib import Path

import pytest
import z3

from asm import assemble
from iss import simulate
from prove import Unsupported, load_contract, min_P, prove
from prove.symex import Byte, SymExec

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "uart_tx.s").read_text()
CONTRACT = load_contract(FW / "uart_tx.contract.yaml")


def test_uart_tx_contract_is_proved_for_the_whole_range():
    out = prove(CONTRACT)
    assert out.proved, out.report()
    assert out.P_range == (8, 65535)
    print(f"\nUART TX proof time: {out.seconds:.2f} s")
    assert out.seconds < 5.0


def test_smallest_P_for_which_the_contract_holds():
    # The contract lets a frame start up to 5 cycles after the previous one ends; with that
    # allowance P = 3 is fine, and P = 2 makes the program late.
    assert min_P(CONTRACT, assemble(SRC)) == 3


BROKEN = {
    "missing stop-bit ADDT": ("        ADDT  P\n        OUT   tx, 1 @T+0", "        OUT   tx, 1 @T+0"),
    "start anchored 1 cycle ahead": ("ADDT  2, now", "ADDT  1, now"),
    "half-period data bits": ("bit:    ADDT  P ", "bit:    ADDT  P/2"),
    "MSB first": ("SHIFT out, lsb, 8", "SHIFT out, msb, 8"),
    "wrong stop level": ("OUT   tx, 1 @T+0         ; stop bit", "OUT   tx, 0 @T+0         ; stop bit"),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_program_gets_an_iss_confirmed_counterexample(name):
    old, new = BROKEN[name]
    assert old in SRC
    out = prove(CONTRACT, assemble(SRC.replace(old, new)))
    assert not out.proved
    assert out.iss_confirms, out.report()
    assert out.failed
    assert "COUNTEREXAMPLE" in out.report()


def test_late_start_is_reported_as_a_late_obligation():
    old, new = BROKEN["start anchored 1 cycle ahead"]
    out = prove(CONTRACT, assemble(SRC.replace(old, new)))
    assert any("LATE" in f for f in out.failed)


# -- the symbolic executor agrees with the ISS ------------------------------------------

def _concrete_trace(events, pin, initial, subst):
    """Pad changes implied by the symbolic drive events under a concrete substitution."""
    final = {}
    for e in sorted(events, key=lambda e: e.key):
        if e.pin == pin:
            t = z3.simplify(z3.substitute(e.time, *subst)).as_long()
            final.setdefault(t, []).append(
                (e.key, int(z3.is_true(z3.simplify(z3.substitute(e.level, *subst))))))
    trace, level = [], initial
    for t in sorted(final):
        v = max(final[t])[1]                     # the event applied last in that cycle wins
        if v != level:
            trace.append((t, v))
            level = v
    return trace


def _program_variants():
    yield "uart_tx", SRC
    for name, (old, new) in BROKEN.items():
        yield name, SRC.replace(old, new)


@pytest.mark.parametrize("name,src", list(_program_variants()))
def test_symbolic_executor_matches_iss(name, src):
    words = assemble(src)
    P = z3.Int("P")
    bs = [Byte(z3.BitVec("d0", 8), z3.Int("a0")), Byte(z3.BitVec("d1", 8), z3.Int("a1"))]
    res = SymExec(words, P, bs).run()
    rng = random.Random(name)
    for _ in range(25):
        p = rng.randint(3, 300)
        d = [rng.randrange(256) for _ in bs]
        a0 = rng.randint(0, 50)
        a1 = a0 + rng.choice([0, rng.randint(0, 30 * p)])
        subst = [(P, z3.IntVal(p)), (bs[0].data, z3.BitVecVal(d[0], 8)),
                 (bs[1].data, z3.BitVecVal(d[1], 8)),
                 (bs[0].arrival, z3.IntVal(a0)), (bs[1].arrival, z3.IntVal(a1))]
        on_time = all(z3.is_true(z3.simplify(z3.substitute(o.cond, *subst)))
                      for o in res.obligations)
        iss = simulate(words, a1 + 40 * p + 200, P=p, initial={0: 0},
                       host_tx=[(a0, d[0]), (a1, d[1])])
        assert iss.ems[0].LATE == (not on_time)
        if on_time:
            assert _concrete_trace(res.events, 0, 0, subst) == iss.pin_trace(0)


def test_unsupported_programs_are_refused_not_guessed():
    with pytest.raises(Unsupported):
        SymExec(assemble("WAIT M0, fall\nHALT"), z3.Int("P"), []).run()


def test_cli(capsys):
    from shruti.__main__ import main
    assert main(["prove", str(FW / "uart_tx.s")]) == 0
    assert "PROVED for P in 8..65535" in capsys.readouterr().out


# -- unbounded proof by induction --------------------------------------------------------

from prove import prove_unbounded  # noqa: E402


def test_uart_tx_is_proved_for_any_number_of_frames():
    out = prove_unbounded(CONTRACT)
    assert out.proved, out.report()
    assert out.base == "holds" and out.step == "holds"


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_programs_fail_the_unbounded_proof(name):
    old, new = BROKEN[name]
    out = prove_unbounded(CONTRACT, SRC.replace(old, new))
    assert not out.proved
    assert out.base != "holds" or out.step != "holds"


def test_an_invariant_that_does_not_hold_is_rejected():
    """The step must fail if the claimed invariant is wrong (here: the stop bit armed at T)."""
    import copy
    c = copy.copy(CONTRACT)
    c.induction = {**CONTRACT.induction, "slots": [["T-2*P", "any"], ["T", 1]]}
    out = prove_unbounded(c)
    assert not out.proved


def test_cli_reports_the_unbounded_proof(capsys):
    from shruti.__main__ import main
    assert main(["prove", str(FW / "uart_tx.s")]) == 0
    assert "PROVED for any number of frames" in capsys.readouterr().out
