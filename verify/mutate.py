"""Planted bugs for the Event Machine's formal properties.

Each mutation edits one spot of a copy of src/shruti_em.v, runs the bounded check
(`em.sby`, task bmc) on it, and must produce a counterexample: that is what shows a property
is not vacuous. The table this prints is the one in verify/README.md.

    python verify/mutate.py                 # all mutations
    python verify/mutate.py halt_sticky     # one, by name

SBY is found as $SBY, else `sby`. Standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

HERE = Path(__file__).resolve().parent
RTL = HERE.parent / "src" / "shruti_em.v"


class Mutation(NamedTuple):
    name: str
    what: str
    old: str
    new: str


MUTATIONS: List[Mutation] = [
    Mutation("out_one_ahead_armed",
             "an OUT due next cycle is armed as a slot (it can never fire)",
             "if (fut_tgt && !one_tgt) begin", "if (fut_tgt) begin"),
    Mutation("one_tgt_carry",
             "the carry-free 'one cycle away' test uses the wrong carry at bit 1",
             "{da_o[22:1], dT[0] & addend[0], 1'b1}", "{da_o[22:1], da_o[0], 1'b1}"),
    Mutation("out_late_at_now",
             "an OUT whose target is the current cycle does not set LATE",
             "if (!fut_tgt) n_late = 1'b1;", "if (!fut_tgt && !eq_tgt) n_late = 1'b1;"),
    Mutation("slot_never_frees",
             "a slot stays busy after it fires",
             "if (fire0) s_busy[0] <= 1'b0;", ""),
    Mutation("in_waits_one_short",
             "a waiting IN@ samples one cycle before its target",
             "end else if (eq_tgt) begin\n                do_sample = 1'b1;",
             "end else if (one_tgt) begin\n                do_sample = 1'b1;"),
    Mutation("wait_timeout_early",
             "a waiting WAIT times out one cycle before its deadline",
             "(blk ? eq_tgt : !fut_tgt)", "(blk ? one_tgt : !fut_tgt)"),
    Mutation("wait_match_dt",
             "WAIT's match sets dT to 0 instead of T - (counter + 1)",
             "t_we = 1'b1; t_val = cnt; d_val = 24'hFFFFFF;",
             "t_we = 1'b1; t_val = cnt; d_val = 24'h000000;"),
    Mutation("rx_overflow",
             "a one-byte push is allowed into a full RX FIFO",
             "(rx_cnt <= 3'd2) : (rx_cnt <= 3'd3)", "(rx_cnt <= 3'd2) : (rx_cnt <= 3'd4)"),
    Mutation("tx_underflow",
             "a two-byte pull is allowed with one byte in the TX FIFO",
             "(tx_cnt >= 3'd2) : (tx_cnt != 3'd0)", "(tx_cnt >= 3'd1) : (tx_cnt != 3'd0)"),
    Mutation("push_silent_drop",
             "a non-blocking PUSH into a full RX FIFO drops the word without OVF",
             "n_ovf = 1'b1;\n            n_isr = 16'd0;", "n_isr = 16'd0;"),
    Mutation("tx_room_before_pull",
             "the TX FIFO judges room before this cycle's pull, not after",
             "tx_push && (tx_after != 3'd4)", "tx_push && (tx_cnt != 3'd4)"),
    Mutation("unf_missing",
             "OUT value=OSR with the OSR empty and no autopull does not set UNF",
             "n_unf = 1'b1;\n                  lvl = o_val[0];", "lvl = o_val[0];"),
    Mutation("autopull_no_block",
             "OUT with autopull goes ahead without TX data",
             "if (!(need_pull && !can_pull)) begin", "if (1'b1) begin"),
    Mutation("illegal_timeout",
             "WAIT timeout 24 is accepted instead of halting",
             "w_tmo > 5'd23) begin", "w_tmo > 5'd24) begin"),
    Mutation("halt_not_sticky",
             "HALT does not latch",
             "if (halt_now) halted <= 1'b1;", ""),
    Mutation("addt_now_tp",
             "ADDT now with d = 0 clears T_PASSED although T is the current cycle",
             "tp_val = (addend == 24'd0);", "tp_val = 1'b0;"),
    Mutation("operand_ignores_host_p",
             "the fetched operand ignores a host write to P's high byte in the same cycle",
             "{p_we_hi ? hdata : (run ? n_P[15:8] : P[15:8]),",
             "{(run ? n_P[15:8] : P[15:8]),"),
    Mutation("out_even_d_now",
             "an on-time OUT with an even target takes effect next cycle, not at T + d",
             "if (fut_tgt && !one_tgt) begin", "if (fut_tgt && !one_tgt && tgt[0]) begin"),
    Mutation("out_both_busy",
             "OUT arms over a busy slot when both slots are busy",
             "if (s_busy != 2'b11) begin", "if (s_busy != 2'b11 || o_val == 2'd1) begin"),
    Mutation("slot1_wrong_pin",
             "slot 1 records the SET pin field instead of the OUT pin",
             "s_pin1 <= arm_pin;", "s_pin1 <= set_pin;"),
    Mutation("slot1_double_fire",
             "slot 1 stays busy when both slots fire in the same cycle",
             "if (fire1) s_busy[1] <= 1'b0;", "if (fire1 && !fire0) s_busy[1] <= 1'b0;"),
    Mutation("slot_refires",
             "a slot that fires re-arms itself two cycles later instead of freeing",
             "if (fire0) s_busy[0] <= 1'b0;", "if (fire0) s_time0 <= s_time0 + 24'd2;"),
    Mutation("slot_drive_dropped",
             "slot drives are dropped while slot 1 is busy",
             "    if (run) begin\n      if (s_ord) begin", "    if (run && !s_busy[1]) begin\n      if (s_ord) begin"),
    Mutation("in_first_early",
             "IN@ in its first cycle samples when the target is one cycle away",
             "if (eq_tgt) do_sample = 1'b1;", "if (eq_tgt || one_tgt) do_sample = 1'b1;"),
    Mutation("wait_timeout_no_to",
             "a WAIT timeout does not set TO",
             "n_to = 1'b1;\n              t_we = 1'b1;", "t_we = 1'b1;"),
    Mutation("wait_match_snapshot",
             "a WAIT match latches last cycle's pin levels into SNAPSHOT",
             "n_snap = vis;", "n_snap = vis_prev;"),
    Mutation("tx_host_overflow",
             "a host write to a full TX FIFO is accepted",
             "tx_push && (tx_after != 3'd4)", "tx_push"),
    Mutation("autopush_drops",
             "IN with autopush completes without pushing when the RX FIFO is full",
             "if (autopush && isr_cnt_e >= in_w) begin", "if (autopush && isr_cnt_e >= in_w && can_push) begin"),
    Mutation("pull_nb_resets_osr",
             "a non-blocking PULL with no data resets OSR_COUNT",
             "end else if (!blockb) begin\n            done = 1'b1;\n          end\n        end\n        // ------------------------------------------------ SET",
             "end else if (!blockb) begin\n            done = 1'b1; n_osr_cnt = 5'd0;\n          end\n        end\n        // ------------------------------------------------ SET"),
    Mutation("illegal_set_drives",
             "SET on an input-only pin halts but still drives a pin",
             "if (s_pin > 4'd7) begin\n                halt_now = 1'b1;",
             "if (s_pin > 4'd7) begin\n                halt_now = 1'b1; set_now = 1'b1;"),
    Mutation("pc_no_wrap",
             "the PC sticks at 31 instead of wrapping to 0",
             "pc <= jump ? jtarget : pc + 5'd1;",
             "pc <= jump ? jtarget : (pc == 5'd31 ? pc : pc + 5'd1);"),
]


def run(m: Mutation, sby: str, work: Path) -> tuple:
    src = RTL.read_text()
    if src.count(m.old) != 1:
        raise SystemExit(f"{m.name}: the text to mutate is not unique in {RTL.name}")
    d = work / m.name
    d.mkdir()
    (d / "shruti_em.v").write_text(src.replace(m.old, m.new))
    cfg = (HERE / "em.sby").read_text().replace("../src/shruti_em.v", "shruti_em.v")
    (d / "em.sby").write_text(cfg)
    t0 = time.time()
    p = subprocess.run([sby, "-f", "em.sby", "bmc"], cwd=d, capture_output=True, text=True)
    secs = time.time() - t0
    out = p.stdout + p.stderr
    status = "FAIL" if "DONE (FAIL" in out else ("PASS" if "DONE (PASS" in out else "ERROR")
    lines = sorted({int(x) for x in re.findall(r"Assert failed in shruti_em: shruti_em\.v:(\d+)", out)})
    return status, lines, secs, out


def describe(src: str, failing: List[int]) -> str:
    """The property IDs of the failing assertions (a trailing `// ID` comment in the RTL;
    an assertion may span lines)."""
    lines, ids = src.splitlines(), []
    for line in failing:
        for k in range(line - 1, min(line + 3, len(lines))):
            m = re.search(r"//\s*([A-Z]\d+)\s*$", lines[k])
            if m:
                ids.append(m.group(1))
                break
    return ", ".join(sorted(set(ids))) or "?"


def main(argv: List[str]) -> int:
    sby = os.environ.get("SBY", "sby")
    chosen = [m for m in MUTATIONS if not argv or m.name in argv]
    work = Path(tempfile.mkdtemp(prefix="em_mut_"))
    bad = 0
    print("| Mutation | Planted bug | Result | Caught by | Seconds |")
    print("| --- | --- | --- | --- | --- |")
    try:
        for m in chosen:
            status, failing, secs, out = run(m, sby, work)
            caught = status == "FAIL"
            bad += not caught
            src = (work / m.name / "shruti_em.v").read_text()
            where = describe(src, failing) if failing else "-"
            print(f"| {m.name} | {m.what} | {'caught' if caught else status} | {where} | {secs:.0f} |",
                  flush=True)
            if status == "ERROR":
                print(out[-2000:], file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
