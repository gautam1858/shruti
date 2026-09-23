/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shruti: an event-native protocol emulator ASIC that also listens.
 *
 * Milestone 0 (flow bring-up): the input path only. Every bus pin goes
 * through a two-flop synchroniser, a per-pin glitch filter with a
 * selectable depth, and an edge detector. This block is the front of the
 * shared event stream; the timestamp counter, the Event Machines, the
 * scheduler, Watch, the Ear and the host SPI land on top of it.
 *
 * Temporary pin use until the host SPI exists (see docs/architecture-spec.md
 * for the final pinout):
 *   ui_in[1:0]   filter mode: 00 bypass, 01 2-of-3 majority, 1x 3-of-5 majority
 *   uio_in[7:0]  bus pins B0..B7, inputs for now
 *   uo_out[7:0]  one-cycle strobe on any edge of the corresponding bus pin
 */

`default_nettype none

module tt_um_gautam1858_shruti (
    input  wire [7:0] ui_in,    // dedicated inputs
    output wire [7:0] uo_out,   // dedicated outputs
    input  wire [7:0] uio_in,   // bidirectional pins, input path
    output wire [7:0] uio_out,  // bidirectional pins, output path
    output wire [7:0] uio_oe,   // bidirectional pins, output enable (1 = drive)
    input  wire       ena,      // 1 while the design is powered
    input  wire       clk,      // core clock, 50 MHz target
    input  wire       rst_n     // active-low reset
);

  wire [1:0] mode = ui_in[1:0];

  // Two-flop synchroniser and a five-deep sample history per pin (hist0 = newest).
  reg [7:0] sync1, sync2;
  reg [7:0] hist0, hist1, hist2, hist3, hist4;
  reg [7:0] filt_q;
  wire [7:0] filt_d;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_pin
      wire [2:0] cnt3 = {2'b00, hist0[i]} + {2'b00, hist1[i]} + {2'b00, hist2[i]};
      wire [2:0] cnt5 = cnt3 + {2'b00, hist3[i]} + {2'b00, hist4[i]};
      assign filt_d[i] = (mode == 2'b00) ? sync2[i]        :  // bypass
                         (mode == 2'b01) ? (cnt3 >= 3'd2)  :  // 2-of-3 majority
                                           (cnt5 >= 3'd3);    // 3-of-5 majority
    end
  endgenerate

  always @(posedge clk) begin
    if (!rst_n) begin
      sync1  <= 8'd0;
      sync2  <= 8'd0;
      hist0  <= 8'd0;
      hist1  <= 8'd0;
      hist2  <= 8'd0;
      hist3  <= 8'd0;
      hist4  <= 8'd0;
      filt_q <= 8'd0;
    end else begin
      sync1  <= uio_in;
      sync2  <= sync1;
      hist0  <= sync2;
      hist1  <= hist0;
      hist2  <= hist1;
      hist3  <= hist2;
      hist4  <= hist3;
      filt_q <= filt_d;
    end
  end

  // Registered one-cycle strobe per pin on any filtered edge.
  reg [7:0] strobe;
  always @(posedge clk) begin
    if (!rst_n) strobe <= 8'd0;
    else        strobe <= filt_d ^ filt_q;
  end

  assign uo_out  = strobe;
  assign uio_out = 8'd0;
  assign uio_oe  = 8'd0;

  // Inputs not used at this milestone.
  wire _unused = &{ena, ui_in[7:2], 1'b0};

endmodule
