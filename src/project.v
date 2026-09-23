/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shruti: an event-native protocol emulator ASIC that also listens.
 *
 * Milestone 1: two Event Machines (src/shruti_em.v) behind the shared input path, the
 * 24-bit timestamp counter, the pin drivers and the host SPI (src/shruti_spi.v). Watch
 * and the Ear come later; their output pins read 0 for now.
 *
 * Pins (docs/architecture-spec.md, "I/O plan and host interface"):
 *   ui_in[0..2]  host SPI SCK, MOSI, CS_n        uo_out[0]  host SPI MISO
 *   ui_in[3]     trigger in (unused so far)      uo_out[1]  IRQ: an Event Machine halted
 *   ui_in[4..7]  monitor-only pins M0-M3         uo_out[2..7] trigger out, Ear (0 for now)
 *   uio[0..7]    bus pins B0-B7, push-pull or open-drain per pin
 *
 * Host register map (8-bit addresses; see docs/host-interface.md):
 *   0x00-0x3F  EM0 program, 32 words, low byte at even addresses; a word is written when
 *              its high (odd) byte arrives
 *   0x40-0x7F  EM1 program
 *   0x80+n     EM0 registers, 0x90+n EM1 registers:
 *     +0 CTRL    bit0 RUN (0 holds the EM in its reset state; 0 -> 1 starts at PC 0);
 *                reads {HALTED, RUN}
 *     +1 FLAGS   reads {UNF, OVF, LATE, TO}; writing 1 clears LATE, OVF, UNF
 *     +2 P[7:0]  +3 P[15:8]
 *     +4 CFG_OUT {AUTOPULL, OUT_MSB, OUT_N[3:0]}   +5 CFG_IN {AUTOPUSH, IN_MSB, IN_N[3:0]}
 *     +6 TX FIFO port (write); reads the TX FIFO count
 *     +7 RX FIFO port (read pops)      +8 RX FIFO count
 *     +9 PC      +A/+B X   +C/+D Y     (read only)
 *   0xA0 SYNC flags [3:0]   0xA1 input filter mode (0 bypass, 1 2-of-3, 2 3-of-5)
 *   0xA2 write: release the pins set in the mask (stop driving them); read: output enables
 *   0xA3 filtered level of B0-B7   0xA4 ID 0x53 ('S')   0xA5 ISA version 0x11
 *   0xA6 RUN for both EMs at once, {EM1, EM0}, so they start in the same cycle
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

  // ------------------------------------------------------------------ timestamp counter
  reg  [23:0] cnt;
  wire [23:0] cnt_p1 = cnt + 24'd1;
  always @(posedge clk) begin
    if (!rst_n) cnt <= 24'd0;
    else        cnt <= cnt_p1;
  end

  // ------------------------------------------------------------------ input path
  // Two-flop synchroniser, a five-deep history and a selectable majority filter per pin.
  // Bypass: a change on the pad in cycle e is seen by the Event Machines in cycle e + 2.
  reg  [1:0]  filt_mode;
  wire [11:0] raw = {ui_in[7:4], uio_in};
  reg  [11:0] sync1, sync2, hist0, hist1, hist2, hist3, hist4, filt_q;
  wire [11:0] filt_d;

  genvar i;
  generate
    for (i = 0; i < 12; i = i + 1) begin : g_pin
      wire [2:0] cnt3 = {2'b00, hist0[i]} + {2'b00, hist1[i]} + {2'b00, hist2[i]};
      wire [2:0] cnt5 = cnt3 + {2'b00, hist3[i]} + {2'b00, hist4[i]};
      assign filt_d[i] = (filt_mode == 2'b00) ? sync2[i]        :  // bypass
                         (filt_mode == 2'b01) ? (cnt3 >= 3'd2)  :  // 2-of-3 majority
                                                (cnt5 >= 3'd3);    // 3-of-5 majority
    end
  endgenerate

  always @(posedge clk) begin
    if (!rst_n) begin
      sync1 <= 12'd0; sync2 <= 12'd0;
      hist0 <= 12'd0; hist1 <= 12'd0; hist2 <= 12'd0; hist3 <= 12'd0; hist4 <= 12'd0;
      filt_q <= 12'd0;
    end else begin
      sync1 <= raw;   sync2 <= sync1;
      hist0 <= sync2; hist1 <= hist0; hist2 <= hist1; hist3 <= hist2; hist4 <= hist3;
      filt_q <= filt_d;
    end
  end

  // ------------------------------------------------------------------ host SPI
  wire       spi_wr;
  wire [7:0] spi_waddr, spi_wdata, spi_raddr;
  reg  [7:0] spi_rdata;
  wire       spi_take;
  wire       spi_miso;
  wire       spi_port = (spi_raddr[7:5] == 3'b100) &&
                        (spi_raddr[3:0] == 4'h6 || spi_raddr[3:0] == 4'h7);

  shruti_spi u_spi (
      .clk(clk), .rst_n(rst_n),
      .sck(ui_in[0]), .mosi(ui_in[1]), .cs_n(ui_in[2]), .miso(spi_miso),
      .wr_stb(spi_wr), .wr_addr(spi_waddr), .wr_data(spi_wdata),
      .rd_addr(spi_raddr), .rd_data(spi_rdata), .rd_take(spi_take), .port(spi_port));

  wire        wr_em0  = spi_wr && spi_waddr[7:4] == 4'h8;
  wire        wr_em1  = spi_wr && spi_waddr[7:4] == 4'h9;
  wire [3:0]  wreg    = spi_waddr[3:0];

  // ------------------------------------------------------------------ program memory
  reg [7:0]  prog_lo;
  reg [15:0] mem0 [0:31];
  reg [15:0] mem1 [0:31];
  always @(posedge clk) begin
    if (spi_wr && !spi_waddr[7]) begin
      if (!spi_waddr[0]) prog_lo <= spi_wdata;
      else if (!spi_waddr[6]) mem0[spi_waddr[5:1]] <= {spi_wdata, prog_lo};
      else                    mem1[spi_waddr[5:1]] <= {spi_wdata, prog_lo};
    end
  end

  // ------------------------------------------------------------------ Event Machines
  reg  [1:0]  run;
  reg  [3:0]  sync;
  wire [4:0]  pc0, pc1, as0, aj0, as1, aj1;
  wire [3:0]  sync_a, sync_b;
  wire [7:0]  en0, lvl0, od0, en1, lvl1, od1;
  wire [7:0]  rxh0, rxh1;
  wire [2:0]  txc0, txc1, rxc0, rxc1;
  wire        halt0, halt1;
  wire [3:0]  fl0, fl1;
  wire [15:0] P0, P1, X0, X1, Y0, Y1;
  wire [5:0]  co0, ci0, co1, ci1;

  wire rd_pop0 = spi_take && spi_raddr == 8'h87;
  wire rd_pop1 = spi_take && spi_raddr == 8'h97;

  shruti_em u_em0 (
      .clk(clk), .rst_n(rst_n), .run(run[0]), .cnt(cnt), .cnt_p1(cnt_p1),
      .vis(filt_d), .vis_prev(filt_q), .pc(pc0),
      .a_seq(as0), .a_jmp(aj0), .d_seq(mem0[as0]), .d_jmp(mem0[aj0]),
      .sync_in(sync), .sync_out(sync_a),
      .drv_en(en0), .drv_lvl(lvl0), .drv_od(od0),
      .hdata(spi_wdata),
      .p_we_lo(wr_em0 && wreg == 4'h2), .p_we_hi(wr_em0 && wreg == 4'h3),
      .cfg_out_we(wr_em0 && wreg == 4'h4), .cfg_in_we(wr_em0 && wreg == 4'h5),
      .flags_clr_we(wr_em0 && wreg == 4'h1),
      .tx_push(wr_em0 && wreg == 4'h6), .rx_pop(rd_pop0),
      .rx_head(rxh0), .tx_cnt(txc0), .rx_cnt(rxc0), .halted(halt0), .flags(fl0),
      .P(P0), .cfg_out(co0), .cfg_in(ci0), .X(X0), .Y(Y0));

`ifdef SHRUTI_SINGLE_EM
  assign sync_b = sync_a;
  assign en1 = 8'd0; assign lvl1 = 8'd0; assign od1 = 8'd0;
  assign pc1 = 5'd0; assign as1 = 5'd0; assign aj1 = 5'd0; assign rxh1 = 8'd0; assign txc1 = 3'd0; assign rxc1 = 3'd0;
  assign halt1 = 1'b0; assign fl1 = 4'd0; assign P1 = 16'd0; assign X1 = 16'd0;
  assign Y1 = 16'd0; assign co1 = 6'd0; assign ci1 = 6'd0;
`else
  shruti_em u_em1 (
      .clk(clk), .rst_n(rst_n), .run(run[1]), .cnt(cnt), .cnt_p1(cnt_p1),
      .vis(filt_d), .vis_prev(filt_q), .pc(pc1),
      .a_seq(as1), .a_jmp(aj1), .d_seq(mem1[as1]), .d_jmp(mem1[aj1]),
      .sync_in(sync_a), .sync_out(sync_b),
      .drv_en(en1), .drv_lvl(lvl1), .drv_od(od1),
      .hdata(spi_wdata),
      .p_we_lo(wr_em1 && wreg == 4'h2), .p_we_hi(wr_em1 && wreg == 4'h3),
      .cfg_out_we(wr_em1 && wreg == 4'h4), .cfg_in_we(wr_em1 && wreg == 4'h5),
      .flags_clr_we(wr_em1 && wreg == 4'h1),
      .tx_push(wr_em1 && wreg == 4'h6), .rx_pop(rd_pop1),
      .rx_head(rxh1), .tx_cnt(txc1), .rx_cnt(rxc1), .halted(halt1), .flags(fl1),
      .P(P1), .cfg_out(co1), .cfg_in(ci1), .X(X1), .Y(Y1));
`endif

  // ------------------------------------------------------------------ pin drivers
  // EM1 wins when both EMs drive a pin in the same cycle. Open-drain: level 0 pulls low,
  // level 1 releases the pin.
  reg [7:0] pin_oe, pin_out;
  integer p;
  always @(posedge clk) begin
    if (!rst_n) begin
      pin_oe <= 8'd0; pin_out <= 8'd0;
    end else begin
      for (p = 0; p < 8; p = p + 1) begin
        if (en1[p]) begin
          pin_oe[p]  <= od1[p] ? !lvl1[p] : 1'b1;
          pin_out[p] <= od1[p] ? 1'b0 : lvl1[p];
        end else if (en0[p]) begin
          pin_oe[p]  <= od0[p] ? !lvl0[p] : 1'b1;
          pin_out[p] <= od0[p] ? 1'b0 : lvl0[p];
        end else if (spi_wr && spi_waddr == 8'hA2 && spi_wdata[p]) begin
          pin_oe[p] <= 1'b0;
        end
      end
    end
  end

  // ------------------------------------------------------------------ control and SYNC
  always @(posedge clk) begin
    if (!rst_n) begin
      run <= 2'b00; sync <= 4'd0; filt_mode <= 2'b00;
    end else begin
      if (wr_em0 && wreg == 4'h0) run[0] <= spi_wdata[0];
      if (wr_em1 && wreg == 4'h0) run[1] <= spi_wdata[0];
      if (spi_wr && spi_waddr == 8'hA6) run <= spi_wdata[1:0];
      if (spi_wr && spi_waddr == 8'hA1) filt_mode <= spi_wdata[1:0];
      sync <= (spi_wr && spi_waddr == 8'hA0) ? spi_wdata[3:0] : sync_b;
    end
  end

  // ------------------------------------------------------------------ host reads
  always @* begin
    case (spi_raddr)
      8'h80: spi_rdata = {6'd0, halt0, run[0]};
      8'h81: spi_rdata = {4'd0, fl0};
      8'h82: spi_rdata = P0[7:0];
      8'h83: spi_rdata = P0[15:8];
      8'h84: spi_rdata = {2'd0, co0};
      8'h85: spi_rdata = {2'd0, ci0};
      8'h86: spi_rdata = {5'd0, txc0};
      8'h87: spi_rdata = rxh0;
      8'h88: spi_rdata = {5'd0, rxc0};
      8'h89: spi_rdata = {3'd0, pc0};
      8'h8A: spi_rdata = X0[7:0];
      8'h8B: spi_rdata = X0[15:8];
      8'h8C: spi_rdata = Y0[7:0];
      8'h8D: spi_rdata = Y0[15:8];
      8'h90: spi_rdata = {6'd0, halt1, run[1]};
      8'h91: spi_rdata = {4'd0, fl1};
      8'h92: spi_rdata = P1[7:0];
      8'h93: spi_rdata = P1[15:8];
      8'h94: spi_rdata = {2'd0, co1};
      8'h95: spi_rdata = {2'd0, ci1};
      8'h96: spi_rdata = {5'd0, txc1};
      8'h97: spi_rdata = rxh1;
      8'h98: spi_rdata = {5'd0, rxc1};
      8'h99: spi_rdata = {3'd0, pc1};
      8'h9A: spi_rdata = X1[7:0];
      8'h9B: spi_rdata = X1[15:8];
      8'h9C: spi_rdata = Y1[7:0];
      8'h9D: spi_rdata = Y1[15:8];
      8'hA0: spi_rdata = {4'd0, sync};
      8'hA1: spi_rdata = {6'd0, filt_mode};
      8'hA2: spi_rdata = pin_oe;
      8'hA3: spi_rdata = filt_d[7:0];
      8'hA4: spi_rdata = 8'h53;
      8'hA5: spi_rdata = 8'h11;
      8'hA6: spi_rdata = {6'd0, run};
      default: spi_rdata = 8'h00;
    endcase
  end

  // ------------------------------------------------------------------ outputs
  assign uo_out  = {5'd0, 1'b0, halt0 | halt1, spi_miso};
  assign uio_out = pin_out;
  assign uio_oe  = pin_oe;

  wire _unused = &{ena, ui_in[3], 1'b0};

endmodule
