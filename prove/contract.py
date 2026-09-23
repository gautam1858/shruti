"""Contracts: YAML files beside the programs in fw/.

    program: uart_tx.s
    assume:
      P: {min: 8, max: 65535}          # symbolic bit period, written by the host
      initial: {B0: 0}                 # external level of undriven pins
      cfg: {}                          # host writes to EMx_CFG, if any
      tx_fifo:                         # symbolic bytes; arrival cycles are symbolic too,
        - {data: d0, at: a0}           # with 0 <= a0 <= a1 <= ...
        - {data: d1, at: a1}
    guarantee:
      never: [LATE, UNF, HALT]
      pin: B0
      idle: {level: 1, from: 1}        # before, between and after frames
      frames:
        for_each: tx_fifo              # one frame per byte, bound to the name d
        start: {within: 5}             # cycles after max(previous frame end, arrival)
        length: "10*P"
        waveform:                      # levels held over [from, to) relative to the start
          - {from: 0, to: P, level: 0}
          - {from: "k*P", to: "(k+1)*P", level: "d[k-1]", k: "1..8"}
          - {from: "9*P", to: "10*P", level: 1}

Expressions are integers, P, the loop variable k, + - * // by constants, max(a, b), and
d[i] (bit i of the frame's byte, as a level).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from iss.isa import load_isa

ISA = load_isa()


class ContractError(Exception):
    pass


@dataclass
class Contract:
    path: Path
    program: Path
    P_min: int
    P_max: int
    initial: Dict[int, int]
    cfg: Dict[str, int]
    fifo: List[Dict[str, str]]
    never: List[str]
    pin: int
    idle_level: int
    idle_from: int
    start_within: int
    length: str
    waveform: List[Dict[str, Any]]
    frames_bound: int
    induction: Optional[Dict[str, Any]] = None

    def with_P_range(self, lo: int, hi: int) -> "Contract":
        c = Contract(**{**self.__dict__})
        c.P_min, c.P_max = lo, hi
        return c


def _pin(name) -> int:
    s = str(name).upper()
    if s in ISA.pin_names:
        return ISA.pin_names[s]
    raise ContractError(f"unknown pin {name!r}")


def load_contract(path) -> Contract:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    try:
        a, g = raw["assume"], raw["guarantee"]
        fr = g["frames"]
        if fr.get("for_each") != "tx_fifo":
            raise ContractError("v0 supports frames: for_each: tx_fifo only")
        return Contract(
            path=path,
            program=path.parent / raw["program"],
            P_min=int(a["P"]["min"]), P_max=int(a["P"]["max"]),
            initial={_pin(k): int(v) for k, v in (a.get("initial") or {}).items()},
            cfg=dict(a.get("cfg") or {}),
            fifo=list(a["tx_fifo"]),
            never=list(g.get("never", [])),
            pin=_pin(g["pin"]),
            idle_level=int(g["idle"]["level"]),
            idle_from=int(g["idle"]["from"]),
            start_within=int(fr["start"]["within"]),
            length=str(fr["length"]),
            waveform=list(fr["waveform"]),
            frames_bound=int(raw.get("bounds", {}).get("frames", len(a["tx_fifo"]))),
            induction=raw.get("induction"),
        )
    except KeyError as e:
        raise ContractError(f"{path}: missing key {e}") from None


def contract_for(program_path) -> Path:
    p = Path(program_path)
    return p.with_name(p.stem + ".contract.yaml")


# -- expressions --------------------------------------------------------------------------

def eval_expr(text, env: Dict[str, Any], level_of_bit):
    """Evaluate a contract expression. env maps names to Z3 terms or ints; `d[i]` calls
    level_of_bit(env['d'], i)."""
    if isinstance(text, (int, bool)):
        return int(text)
    tree = ast.parse(str(text), mode="eval").body

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in env:
                raise ContractError(f"unknown name {n.id!r} in {text!r}")
            return env[n.id]
        if isinstance(n, ast.BinOp):
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return l + r
            if isinstance(n.op, ast.Sub):
                return l - r
            if isinstance(n.op, ast.Mult):
                return l * r
            if isinstance(n.op, ast.FloorDiv):
                return l / r if not isinstance(l, int) else l // r
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "max":
            import z3
            a, b = ev(n.args[0]), ev(n.args[1])
            return z3.If(a >= b, a, b)
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "d":
            return level_of_bit(env["d"], ev(n.slice))
        raise ContractError(f"unsupported expression {ast.unparse(n)!r} in {text!r}")

    return ev(tree)


def expand_waveform(waveform) -> List[Dict[str, Any]]:
    """Expand `k: "a..b"` entries into one entry per k."""
    out = []
    for seg in waveform:
        if "k" in seg:
            a, b = (int(x) for x in str(seg["k"]).split(".."))
            for k in range(a, b + 1):
                out.append({**seg, "_k": k})
        else:
            out.append(seg)
    return out
