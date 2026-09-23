"""Receiver contracts: fw/uart_rx.s against a symbolic 8N1 peer with edge jitter."""

import copy
from pathlib import Path

import pytest

from asm import assemble
from prove.reactive import load_receiver, prove_receiver

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "uart_rx.s").read_text()
C = load_receiver(FW / "uart_rx.contract.yaml")


def test_uart_rx_contract_is_proved():
    out = prove_receiver(C)
    assert out.proved, out.report()
    assert out.P_range == (16, 65535)


BROKEN = {
    "7 data bits": ("        SET   X, 7", "        SET   X, 6"),
    "no wait for the stop bit": (
        "        ADDT  P                  ; middle of the stop bit\n"
        "        IN    rx @T+0            ; wait until then (ISR is full, the sample is dropped)\n", ""),
    "sampling at the bit edge": ("        ADDT  P/2                ; middle of the start bit",
                                 "        ADDT  P                  ; middle of the start bit"),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_receiver_gets_an_iss_confirmed_counterexample(name):
    old, new = BROKEN[name]
    assert old in SRC
    out = prove_receiver(C, assemble(SRC.replace(old, new)))
    assert not out.proved and out.iss_confirms, out.report()


def test_too_fast_a_bit_period_is_found_and_matches_the_iss_limit():
    """The ISS measured B >= 9; the prover finds the failure at 8 and proves 9 upwards."""
    assert not prove_receiver(C, P_range=(8, 8)).proved
    assert prove_receiver(C, P_range=(9, 65535)).proved


def test_jitter_limit():
    """Mid-bit sampling tolerates edges moved by up to just under half a bit."""
    c = copy.copy(C)
    c.jitter = "P//3"
    assert prove_receiver(c).proved
    c.jitter = "P//2 + 2"
    out = prove_receiver(c)
    assert not out.proved and out.iss_confirms, out.report()


def test_cli_proves_a_receiver(capsys):
    from shruti.__main__ import main
    assert main(["prove", str(FW / "uart_rx.s")]) == 0
    assert "PROVED" in capsys.readouterr().out
