"""Signoff gate on the GDS action's post-route metrics (LibreLane runs/wokwi/final/metrics.json).

The GDS job reports success even when timing fails, so CI runs this on the GDS_logs
artifact. It fails on:
- negative setup or hold slack, or any setup or hold violation, at any corner;
- any routing DRC error, any antenna violation, any Magic DRC error or any LVS error;
- a metric it needs that is missing (an empty report must not pass).
It warns when the slow-corner setup slack is under the project's margin (+4.0 ns), when the
worst hold slack is under the flow's GRT_RESIZER_HOLD_SLACK_MARGIN (src/config.json), and
when there are max-slew, max-cap or max-fanout violations, which the flow reports as
warnings. The table it prints is also written to $GITHUB_STEP_SUMMARY when that is set.

    python verify/signoff.py runs/wokwi/final/metrics.json --config src/config.json

Exit code 0 on pass (warnings included), 1 on fail. Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SETUP_MARGIN_NS = 4.0          # slow-corner setup slack the project holds itself to
CORNER = "__corner:"


@dataclass
class Verdict:
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)   # the summary table

    @property
    def passed(self) -> bool:
        return not self.failures


def corners(m: Dict[str, float]) -> List[str]:
    """Corner names, from the per-corner setup slack keys."""
    prefix = "timing__setup__ws" + CORNER
    return sorted(k[len(prefix):] for k in m if k.startswith(prefix))


def hold_margin(config_path: Optional[Path]) -> float:
    """GRT_RESIZER_HOLD_SLACK_MARGIN from the flow config: the hold slack the router-stage
    resizer repairs to. src/config.json repeats its "//" comment keys, which json allows."""
    if config_path is None or not config_path.exists():
        return 0.05
    return float(json.loads(config_path.read_text()).get("GRT_RESIZER_HOLD_SLACK_MARGIN", 0.05))


def _ns(v: float) -> str:
    return f"{v:+.2f} ns"


def check(m: Dict[str, float], hold_target: float = 0.05,
          setup_margin: float = SETUP_MARGIN_NS) -> Verdict:
    v = Verdict()

    def need(key: str) -> Optional[float]:
        if key not in m or m[key] is None:
            v.failures.append(f"metric {key} is missing")
            return None
        return float(m[key])

    names = corners(m)
    if not names:
        v.failures.append("no per-corner timing metrics found")
    slow = [c for c in names if "slow" in c]

    # timing, per corner
    for c in names:
        s = need("timing__setup__ws" + CORNER + c)
        h = need("timing__hold__ws" + CORNER + c)
        sv = need("timing__setup_vio__count" + CORNER + c)
        hv = need("timing__hold_vio__count" + CORNER + c)
        if s is None or h is None or sv is None or hv is None:
            continue
        if s < 0 or sv > 0:
            v.failures.append(f"setup: {c} slack {_ns(s)}, {int(sv)} violations")
        if h < 0 or hv > 0:
            v.failures.append(f"hold: {c} slack {_ns(h)}, {int(hv)} violations")
        v.rows.append([f"Setup slack, {c}", _ns(s), f"{int(sv)} violations"])
        v.rows.append([f"Hold slack, {c}", _ns(h), f"{int(hv)} violations"])
    for c in slow:
        s = m.get("timing__setup__ws" + CORNER + c)
        if s is not None and 0 <= s < setup_margin:
            v.warnings.append(f"slow-corner setup slack {_ns(s)} at {c} is under the "
                              f"{_ns(setup_margin)} margin")
    worst_hold = min((m["timing__hold__ws" + CORNER + c] for c in names
                      if ("timing__hold__ws" + CORNER + c) in m), default=None)
    if worst_hold is not None and 0 <= worst_hold < hold_target:
        v.warnings.append(f"worst hold slack {worst_hold:+.4f} ns is under "
                          f"GRT_RESIZER_HOLD_SLACK_MARGIN ({hold_target:.4f} ns)")

    # physical checks that must be clean
    for key, what in [("route__drc_errors", "routing DRC errors"),
                      ("route__antenna_violation__count", "antenna violations"),
                      ("antenna__violating__nets", "nets with antenna violations")]:
        n = need(key)
        if n is not None:
            if n > 0:
                v.failures.append(f"{int(n)} {what}")
            v.rows.append([what[0].upper() + what[1:], str(int(n)), ""])
    for key, what in [("magic__drc_error__count", "Magic DRC errors"),
                      ("design__lvs_error__count", "LVS errors")]:
        if key in m:                       # not every flow configuration runs these
            n = float(m[key])
            if n > 0:
                v.failures.append(f"{int(n)} {what}")
            v.rows.append([what, str(int(n)), ""])

    # electrical warnings
    for key, what in [("design__max_slew_violation__count", "max-slew"),
                      ("design__max_cap_violation__count", "max-cap"),
                      ("design__max_fanout_violation__count", "max-fanout")]:
        n = m.get(key)
        if n is None:
            continue
        v.rows.append([f"{what[0].upper() + what[1:]} violations", str(int(n)), "warning"])
        if n > 0:
            v.warnings.append(f"{int(n)} {what} violations")

    # area and power, reported only
    util = m.get("design__instance__utilization")
    if util is not None:
        v.rows.append(["Utilisation", f"{100 * util:.1f}%", ""])
    for key, what in [("design__instance__area__stdcell", "Standard-cell area"),
                      ("design__instance__area__class:timing_repair_buffer",
                       "Timing-repair buffer area"),
                      ("design__instance__area__class:sequential_cell", "Flip-flop area")]:
        if key in m:
            v.rows.append([what, f"{float(m[key]):,.0f} um2", ""])
    if "design__instance__count__class:sequential_cell" in m:
        v.rows.append(["Flip-flops", f"{int(m['design__instance__count__class:sequential_cell']):,}", ""])
    if "power__total" in m:
        v.rows.append(["Power, total", f"{1000 * float(m['power__total']):.1f} mW", ""])
    return v


def markdown(v: Verdict) -> str:
    head = "PASS" if v.passed else "FAIL"
    lines = [f"## Signoff: {head}", "", "| Metric | Value | Note |", "| --- | --- | --- |"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in v.rows]
    if v.failures:
        lines += ["", "Failures:", ""] + [f"- {f}" for f in v.failures]
    if v.warnings:
        lines += ["", "Warnings:", ""] + [f"- {w}" for w in v.warnings]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("metrics", type=Path)
    ap.add_argument("--config", type=Path, default=Path("src/config.json"))
    ap.add_argument("--setup-margin", type=float, default=SETUP_MARGIN_NS)
    a = ap.parse_args(argv)
    if not a.metrics.exists():
        print(f"::error::{a.metrics} not found")
        return 1
    v = check(json.loads(a.metrics.read_text()), hold_margin(a.config), a.setup_margin)
    md = markdown(v)
    print(md)
    for f in v.failures:
        print(f"::error::signoff: {f}")
    for w in v.warnings:
        print(f"::warning::signoff: {w}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(md)
    return 0 if v.passed else 1


if __name__ == "__main__":
    sys.exit(main())
