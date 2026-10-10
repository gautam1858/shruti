"""The signoff gate against a real post-route metrics.json (GDS run of commit fcafc0b) and a
doctored copy of it that must fail."""

import json
from pathlib import Path

from verify import signoff

DATA = Path(__file__).parent / "data"
CONFIG = Path(__file__).parents[2] / "src" / "config.json"


def load(name):
    return json.loads((DATA / name).read_text())


def test_real_run_passes_with_its_known_warnings():
    v = signoff.check(load("metrics_fcafc0b.json"), signoff.hold_margin(CONFIG))
    assert v.passed, v.failures
    text = " ".join(v.warnings)
    assert "10 max-slew" in text and "1 max-cap" in text and "308 max-fanout" in text
    assert "worst hold slack +0.0495 ns" in text        # against a 0.05 ns margin
    assert not any("slow-corner" in w for w in v.warnings)   # +4.24 ns clears +4.0 ns


def test_real_run_table_has_every_corner_and_the_areas():
    md = signoff.markdown(signoff.check(load("metrics_fcafc0b.json")))
    for corner in ("nom_slow_1p08V_125C", "nom_typ_1p20V_25C", "nom_fast_1p32V_m40C"):
        assert f"Setup slack, {corner}" in md and f"Hold slack, {corner}" in md
    assert "| Setup slack, nom_slow_1p08V_125C | +4.24 ns |" in md
    assert "| Utilisation | 68.6% |" in md
    assert "| Timing-repair buffer area | 113,131 um2 |" in md
    assert md.startswith("## Signoff: PASS")


def test_doctored_run_fails_on_each_problem():
    v = signoff.check(load("metrics_doctored.json"), signoff.hold_margin(CONFIG))
    assert not v.passed
    text = " | ".join(v.failures)
    assert "setup: nom_slow_1p08V_125C slack -0.31 ns, 4 violations" in text
    assert "hold: nom_fast_1p32V_m40C slack -0.02 ns, 1 violations" in text
    assert "2 routing DRC errors" in text


def test_main_exit_codes(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert signoff.main([str(DATA / "metrics_fcafc0b.json"), "--config", str(CONFIG)]) == 0
    assert signoff.main([str(DATA / "metrics_doctored.json"), "--config", str(CONFIG)]) == 1
    assert signoff.main([str(tmp_path / "missing.json")]) == 1
    assert summary.read_text().count("## Signoff:") == 2


def test_single_problems_are_caught():
    base = load("metrics_fcafc0b.json")
    cases = {
        "route__antenna_violation__count": 1,
        "antenna__violating__nets": 1,
        "magic__drc_error__count": 3,
        "design__lvs_error__count": 1,
        "timing__hold_vio__count__corner:nom_typ_1p20V_25C": 2,
    }
    for key, value in cases.items():
        m = dict(base, **{key: value})
        assert not signoff.check(m).passed, key


def test_slow_margin_warning_and_missing_metrics():
    m = dict(load("metrics_fcafc0b.json"))
    m["timing__setup__ws__corner:nom_slow_1p08V_125C"] = 3.1
    v = signoff.check(m)
    assert v.passed and any("under the +4.00 ns margin" in w for w in v.warnings)
    del m["route__drc_errors"]
    assert "metric route__drc_errors is missing" in signoff.check(m).failures
    assert not signoff.check({}).passed


def test_hold_margin_is_read_from_the_flow_config():
    assert signoff.hold_margin(CONFIG) == 0.05
