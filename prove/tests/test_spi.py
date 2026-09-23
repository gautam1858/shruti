"""SPI master contract: fw/spi_master.s against a symbolic mode-0 slave."""

from pathlib import Path

import pytest

from asm import assemble
from prove.spi import load_spi, prove_spi

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "spi_master.s").read_text()
C = load_spi(FW / "spi_master.contract.yaml")


def test_spi_master_contract_is_proved():
    out = prove_spi(C)
    assert out.proved, out.report()


def test_half_period_of_4_is_late_as_on_the_iss():
    out = prove_spi(C, P_range=(4, 4))
    assert not out.proved and any("LATE" in f for f in out.failed)


BROKEN = {
    "LSB first": ("SHIFT out, msb, 8", "SHIFT out, lsb, 8"),
    "MISO sampled after the edge": ("        IN    miso @T+0          ; sample MISO at the rising edge",
                                    "        IN    miso @T+3          ; sample MISO at the rising edge"),
    "CS released too early": ("        ADDT  P\n        OUT   cs, 1 @T+0         ; deselect",
                              "        OUT   cs, 1 @T+0         ; deselect"),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_master_gets_a_counterexample(name):
    old, new = BROKEN[name]
    assert old in SRC
    out = prove_spi(C, assemble(SRC.replace(old, new)))
    assert not out.proved and out.failed, out.report()
