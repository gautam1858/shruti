"""SPI master contract: fw/spi_master.s in modes 0-3 against a symbolic slave."""

from pathlib import Path

import pytest

from asm import assemble
from prove.spi import load_spi, mode_defines, prove_spi

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "spi_master.s").read_text()
C = load_spi(FW / "spi_master.contract.yaml")


def build(mode, src=SRC):
    return assemble(src, "spi_master.s", mode_defines(mode))


def test_the_contract_lists_all_four_modes():
    assert C.modes == [0, 1, 2, 3]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_master_contract_is_proved(mode):
    out = prove_spi(C, build(mode), mode=mode)
    assert out.proved, out.report()


@pytest.mark.parametrize("mode", [0, 3])
def test_half_period_of_4_is_late_as_on_the_iss(mode):
    out = prove_spi(C, build(mode), P_range=(4, 4), mode=mode)
    assert not out.proved and any("LATE" in f for f in out.failed)


BROKEN = {     # (mode, old text, new text)
    "LSB first": (0, "SHIFT out, msb, 8", "SHIFT out, lsb, 8"),
    "MISO sampled after the edge": (
        0, "        IN    miso @T+0          ; sample MISO at the leading edge",
        "        IN    miso @T+3          ; sample MISO at the leading edge"),
    "CS released too early": (0, "        ADDT  P\n        OUT   cs, 1 @T+0         ; deselect",
                              "        OUT   cs, 1 @T+0         ; deselect"),
    "CPHA 1 samples on the leading edge": (
        1, "        OUT   sck, IDLE @T+0     ; trailing edge\n        IN    miso @T+0          ; sample MISO at the trailing edge",
        "        IN    miso @T+0          ; sample MISO at the trailing edge\n        OUT   sck, IDLE @T+0     ; trailing edge"),
    "CPOL 1 starts with SCK low": (2, "        SET   sck, IDLE", "        SET   sck, ACTIVE"),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_master_gets_a_counterexample(name):
    mode, old, new = BROKEN[name]
    assert old in SRC
    out = prove_spi(C, build(mode, SRC.replace(old, new)), mode=mode)
    assert not out.proved and out.failed, out.report()
