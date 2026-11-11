#!/bin/sh
# Build the Tang Nano 20K bitstream with the open Gowin toolchain: Yosys synth_gowin,
# nextpnr-himbaechel (Gowin) and Apicula's gowin_pack. Run from the repo root. Uses the
# native tools if they are on PATH, else the YoWASP builds (pip install yowasp-yosys
# yowasp-nextpnr-himbaechel-gowin apycula).
set -eu
OUT=fpga/tangnano20k/out
mkdir -p "$OUT"
YOSYS=$(command -v yosys || command -v yowasp-yosys)
NEXTPNR=$(command -v nextpnr-himbaechel || command -v yowasp-nextpnr-himbaechel-gowin)
SRC="src/project.v src/shruti_em.v src/shruti_spi.v src/shruti_ear.v src/shruti_watch.v"
"$YOSYS" -q -l "$OUT/synth.log" -p "read_verilog -sv $SRC fpga/tangnano20k/top.v; synth_gowin -top top -json $OUT/shruti.json"
"$NEXTPNR" --json "$OUT/shruti.json" --write "$OUT/pnr.json" \
  --device GW2AR-LV18QN88C8/I7 --vopt family=GW2A-18C --vopt cst=fpga/tangnano20k/shruti.cst \
  --freq 27 --log "$OUT/pnr.log"
gowin_pack -d GW2A-18C -o "$OUT/shruti.fs" "$OUT/pnr.json"
echo "bitstream: $OUT/shruti.fs"
