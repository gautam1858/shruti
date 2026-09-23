#!/usr/bin/env bash
# Early synthesis of src/ onto the IHP CMOS5L standard cells: cell count and cell area.
# This is Yosys mapping only (no placement, clock tree or routing); the GDS action in
# .github/workflows runs the full LibreLane flow and is the number that counts.
#
# Needs PDK_ROOT (as the Tiny Tapeout flow sets it) and yosys or yowasp-yosys. Paths are
# kept relative to the repo root because yowasp-yosys only sees the working directory.
set -euo pipefail
cd "$(dirname "$0")/.."
lib=${PDK_ROOT:?set PDK_ROOT}/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/sg13cmos5l_stdcell_typ_1p20V_25C.lib
yosys=$(command -v yosys || command -v yowasp-yosys)
out=synth/out
mkdir -p "$out"
cp "$lib" "$out/cells.lib"
sed "s|@LIB@|$out/cells.lib|g; s|@OUT@|$out|g" synth/synth.ys > "$out/synth.ys"
"$yosys" -q -l "$out/synth.log" "$out/synth.ys" > /dev/null
awk '/Printing statistics/{n++} n==2' "$out/synth.log" | grep -E "cells$|Chip area|dfrbp"
