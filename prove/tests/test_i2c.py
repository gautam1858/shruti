"""I2C master contract: fw/i2c_master.s against a symbolic slave (ACK/NACK, stretching)."""

from pathlib import Path

import pytest

from prove.i2c import load_i2c, prove_i2c

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "i2c_master.s").read_text()
C = load_i2c(FW / "i2c_master.contract.yaml")


@pytest.mark.parametrize("mode", ["standard", "fast"])
def test_i2c_master_contract_is_proved(mode):
    out = prove_i2c(C, mode=mode)
    assert out.proved, out.report()
    assert out.paths >= 3                    # at least: ACK, ACK / ACK, NACK / NACK


def test_a_short_high_phase_is_rejected_in_fast_mode():
    old = ".define THIGH P/2\n"
    assert old in SRC
    out = prove_i2c(C, SRC.replace(old, ".define THIGH P/4\n"), P_range=(65, 200), mode="fast")
    assert not out.proved
    assert any("SCL high at least P/2" in f for f in out.failed), out.report()
    assert any("START: SCL stays high" in f for f in out.failed), out.report()


def test_the_fast_shape_fails_the_standard_contract():
    old = ".define THIGH P\n"
    assert old in SRC
    out = prove_i2c(C, SRC.replace(old, ".define THIGH P/2\n"), P_range=(65, 200),
                    mode="standard")
    assert not out.proved
    assert any("SCL high at least P" in f for f in out.failed), out.report()


def test_sda_changing_with_the_scl_fall_is_rejected():
    old = "        OUT   sda, OSR @T+8      ; data bit, 8 cycles after SCL falls"
    assert old in SRC
    out = prove_i2c(C, SRC.replace(old, old.replace("@T+8", "@T+0")))
    assert not out.proved
    assert any("SDA correct and stable" in f for f in out.failed), out.report()
