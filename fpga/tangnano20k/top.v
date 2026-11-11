/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shruti on a Sipeed Tang Nano 20K (GW2AR-18C): the same RTL as the ASIC (src/), clocked
 * from the board's 27 MHz oscillator. Cycle counts are core cycles, so programs run
 * unchanged; bit periods (P) and baud rates scale with the clock. Pins are in shruti.cst.
 */

`default_nettype none

module top (
    input  wire       clk27,       // 27 MHz oscillator
    input  wire       btn_rst,     // S1: pressed = reset
    // host SPI, from an external SPI master (an RP2040 board)
    input  wire       spi_sck,
    input  wire       spi_mosi,
    input  wire       spi_cs_n,
    output wire       spi_miso,
    // bus pins B0-B7, push-pull or open-drain per pin as the Event Machines set them
    inout  wire [7:0] bus,
    // monitor-only pins M0-M3, Watch trigger-in and trigger-out
    input  wire [3:0] mon,
    input  wire       trig_in,
    output wire       trig_out,
    // the on-board USB-UART bridge: B1, the Pmod UART TXD pin, also goes to the PC
    output wire       usb_uart_tx,
    // LEDs, active low: IRQ, Ear confident, Ear class (3 bits), Ear OOD
    output wire [5:0] led_n
);

  // Reset: two flops so the button's release is synchronous.
  reg [1:0] rst_sync = 2'b00;
  always @(posedge clk27) rst_sync <= {rst_sync[0], !btn_rst};
  wire rst_n = rst_sync[1];

  wire [7:0] uo_out, uio_out, uio_oe;

  tt_um_gautam1858_shruti u_shruti (
      .ui_in  ({mon, trig_in, spi_cs_n, spi_mosi, spi_sck}),
      .uo_out (uo_out),
      .uio_in (bus),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk27),
      .rst_n  (rst_n));

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_bus
      assign bus[i] = uio_oe[i] ? uio_out[i] : 1'bz;
    end
  endgenerate

  assign spi_miso    = uo_out[0];
  assign trig_out    = uo_out[2];
  assign usb_uart_tx = uio_oe[1] ? uio_out[1] : 1'b1;      // idle high when not driven
  assign led_n       = ~{uo_out[7], uo_out[6:4], uo_out[3], uo_out[1]};

endmodule
