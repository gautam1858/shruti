"""python -m shruti prove <program.s> [--contract file] [--min-P]

Proves an Event Machine program against its contract (by default the .contract.yaml beside
it). Exit status 0 on a proof, 1 on a counterexample, 2 on a usage or input error.
"""

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shruti")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prove", help="prove a program against its contract")
    p.add_argument("program")
    p.add_argument("--contract", help="contract file (default: <program>.contract.yaml)")
    p.add_argument("--min-P", action="store_true",
                   help="also report the smallest P for which the contract holds")
    a = ap.parse_args(argv)

    from asm import AsmError, assemble
    from prove import ContractError, contract_for, load_contract, min_P, prove, Unsupported

    try:
        cpath = Path(a.contract) if a.contract else contract_for(a.program)
        c = load_contract(cpath)
        words = assemble(Path(a.program).read_text(), a.program)
        out = prove(c, words)
    except (AsmError, ContractError, Unsupported, FileNotFoundError) as e:
        print(f"shruti prove: {e}", file=sys.stderr)
        return 2
    print(f"{a.program} against {cpath}:")
    print(out.report())
    if a.min_P and out.proved:
        print(f"smallest P for which the contract holds: {min_P(c, words)}")
    return 0 if out.proved else 1


if __name__ == "__main__":
    sys.exit(main())
