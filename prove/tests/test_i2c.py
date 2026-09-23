"""I2C master contract: fw/i2c_master.s against a symbolic slave (ACK/NACK, stretching)."""

from pathlib import Path

from prove.i2c import load_i2c, prove_i2c

FW = Path(__file__).resolve().parents[2] / "fw"
SRC = (FW / "i2c_master.s").read_text()
C = load_i2c(FW / "i2c_master.contract.yaml")


def test_i2c_master_contract_is_proved():
    out = prove_i2c(C)
    assert out.proved, out.report()
    assert out.paths >= 3                    # at least: ACK, ACK / ACK, NACK / NACK


def test_sda_changing_with_the_scl_fall_is_rejected():
    old = "        OUT   sda, OSR @T+8      ; data bit, 8 cycles after SCL falls"
    assert old in SRC
    out = prove_i2c(C, SRC.replace(old, old.replace("@T+8", "@T+0")))
    assert not out.proved
    assert any("SDA correct and stable" in f for f in out.failed), out.report()
