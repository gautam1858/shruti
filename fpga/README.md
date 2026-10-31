# fpga: Tang Nano 20K prototype (planned)

Nothing is built here yet. The plan, from the spec's milestones (FPGA bring-up 6 Dec 2026):

- the same RTL and firmware on a Tang Nano 20K, with a PLL from its 27 MHz oscillator and an external SPI master (an RP2040 board) as the host;
- built with the open toolchain: Yosys `synth_gowin`, nextpnr-himbaechel and Apicula;
- against real devices: a USB-UART adapter, an SPI flash and an I2C sensor first, then the Ethernet link-pulse test.

Nothing about real devices is claimed until hardware runs. `.github/workflows/fpga.yaml` is still the Tiny Tapeout template's iCE40UP5K simulation flow, disabled on push; it is replaced when the Tang Nano build exists, and the README badge comes back when that workflow runs green.
