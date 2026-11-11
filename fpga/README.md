# fpga: Tang Nano 20K prototype

`tangnano20k/` builds Shruti for a Sipeed Tang Nano 20K (Gowin GW2AR-18C) with the open toolchain: Yosys `synth_gowin`, nextpnr-himbaechel and Apicula's `gowin_pack`. The RTL is `src/` unchanged; `tangnano20k/top.v` adds only the pad buffers, a reset synchroniser on button S1 and the LED inversion.

```
sh fpga/tangnano20k/build.sh        # from the repo root; writes fpga/tangnano20k/out/shruti.fs
```

The `fpga` workflow builds it on every push and keeps the bitstream and the logs.

Measured (commit e44b498 plus this directory, Nov 2026), on the 27 MHz board clock:

| Resource | Used | Of |
| --- | --- | --- |
| LUT4 | 12,483 (60%) | 20,736 |
| Flip-flops | 3,893 (25%) | 15,552 |
| ALU | 3,462 (22%) | 15,552 |
| I/O | 19 | 384 |
| Block RAM | 0 | 46 |

nextpnr's timing estimate is 70.1 MHz, so the board's 27 MHz clock passes and so would the ASIC's 50 MHz through the PLL (not added yet). Cycle counts are core cycles, so programs run unchanged; bit periods (P) and baud rates scale with the clock.

## Pins

| Signal | Pin | From |
| --- | --- | --- |
| 27 MHz clock | 4 | Sipeed's examples |
| Reset (button S1, pressed = reset) | 88 | Sipeed's examples |
| LEDs, active low: IRQ, Ear confident, Ear class (3), Ear OOD | 15-20 | Sipeed's examples |
| B1 (Pmod UART TXD) to the on-board USB-UART | 69 | Sipeed's examples |
| Host SPI SCK, MOSI, CS_n, MISO | 52, 53, 71, 72 | pins Sipeed's examples bring out to a joystick adapter |
| B0-B7 | 27-34 | pins Sipeed's examples bring out to the RGB LCD connector |
| M0-M3, trigger-in, trigger-out | 35-38, 39, 40 | the same |

The first four rows are the pins Sipeed's examples (github.com/sipeed/TangNano-20K-example) use for those parts. The others are pins those examples bring out to external parts; check them against the board schematic before wiring anything to them.

## Not done yet

- Nothing has run on a board: there is a bitstream and a timing estimate, not a measurement. Nothing about real devices is claimed until hardware runs.
- The PLL to 50 MHz, the host software on an RP2040, and the tests against a USB-UART adapter, an SPI flash and an I2C sensor (spec milestone, FPGA bring-up 6 Dec 2026).
