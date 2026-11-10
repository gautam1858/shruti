/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * Watch, cut level: 2 rule monitors, the trigger and a 16-entry flight recorder. Cycle
 * exact to watch/model.py as defined in watch/SPEC.md. Watch only observes: it reads the
 * filtered pins and drives trigger-out, IRQ and SYNC flag requests.
 *
 * In cycle t: a monitor matches on an edge of its pin with the selected direction, the
 * guard over the 12 levels, and the interval since that pin's previous edge within its
 * log-code bounds. The trigger fires on an enabled source while armed and not triggered.
 * Every cycle with an edge on a recorded pin writes {levels, timestamp[15:0]} while armed
 * and not frozen; after the trigger, POST more entries are kept, then the recorder freezes.
 * Host writes come last in the register block, so they win over the cycle's own updates.
 */

`default_nettype none

module shruti_watch (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [11:0] vis,          // filtered pins this cycle
    input  wire [11:0] vis_prev,     // and last cycle
    input  wire [23:0] cnt,          // timestamp
    input  wire        ear_ood,      // the Ear's registered OOD flag
    input  wire [2:0]  ear_shown,    // the Ear's class pins
    input  wire        trig_pad,     // trigger-in pad (ui_in[3]), asynchronous
    // host
    input  wire        wr,
    input  wire [7:0]  waddr,
    input  wire [7:0]  wdata,
    input  wire [7:0]  raddr,
    input  wire        rtake,        // the host clocked the first bit of the byte at raddr
    output reg  [7:0]  rdata,
    // outputs
    output wire [3:0]  sync_set,     // SYNC flags to set for the EMs in the next cycle
    output wire        trig_out,
    output wire        irq
);

  // ------------------------------------------------------------------ the Ear's log code
  function [5:0] log2_code(input [15:0] i);
    integer b;
    reg [3:0] m;
    reg [1:0] mant;
    begin
      m = 4'd0;
      for (b = 1; b < 16; b = b + 1)
        if (i[b]) m = b[3:0];
      mant[1] = (m >= 4'd1) ? i[m - 4'd1] : 1'b0;
      mant[0] = (m >= 4'd2) ? i[m - 4'd2] : 1'b0;
      log2_code = {m, mant};
    end
  endfunction

  // ------------------------------------------------------------------ state
  reg [3:0]  pin   [0:1];
  reg [1:0]  edsel [0:1];            // 0 off, 1 rise, 2 fall, 3 any
  reg        a_sync[0:1];
  reg [1:0]  sidx  [0:1];
  reg [11:0] mask  [0:1];
  reg [11:0] value [0:1];
  reg [5:0]  cmin  [0:1];
  reg [5:0]  cmax  [0:1];
  reg [15:0] since [0:1];
  reg [7:0]  hits  [0:1];

  reg        armed, triggered, frozen;
  reg [4:0]  src, cause;
  reg [3:0]  post, left;
  reg [23:0] ttime;
  reg [11:0] pins;
  reg [27:0] mem [0:15];
  reg [3:0]  wptr;
  reg [4:0]  count;
  reg [5:0]  rbyte;
  reg        tin1, tin2, tin3, ood_prev;
  reg [2:0]  shown_prev;

  // ------------------------------------------------------------------ this cycle
  wire [11:0] edges = vis ^ vis_prev;

  wire [1:0] match;
  wire [1:0] pin_edge;
  genvar g;
  generate
    for (g = 0; g < 2; g = g + 1) begin : g_mon
      wire       valid_pin = pin[g] < 4'd12;
      wire       e   = valid_pin && edges[pin[g]];
      wire       lvl = vis[pin[g]];
      wire       dir = (edsel[g] == 2'd3) || (edsel[g] == 2'd1 && lvl) ||
                       (edsel[g] == 2'd2 && !lvl);
      wire       grd = ((vis ^ value[g]) & mask[g]) == 12'd0;
      wire [5:0] code = log2_code(since[g]);
      wire       bnd = code >= cmin[g] && code <= cmax[g];
      assign pin_edge[g] = e;
      assign match[g]    = edsel[g] != 2'd0 && e && dir && grd && bnd;
    end
  endgenerate

  wire [4:0] sources = {tin2 && !tin3, ear_shown != shown_prev, ear_ood && !ood_prev,
                        match[1], match[0]};
  wire [4:0] fired     = sources & src;
  wire       ctrl_wr   = wr && waddr == 8'hE0;
  wire       host_trig = ctrl_wr && wdata[1];
  wire       fire      = armed && !triggered && (fired != 5'd0 || host_trig);
  wire       record    = armed && !frozen && (edges & pins) != 12'd0;

  assign sync_set = ((match[0] && a_sync[0]) ? (4'd1 << sidx[0]) : 4'd0) |
                    ((match[1] && a_sync[1]) ? (4'd1 << sidx[1]) : 4'd0);
  assign trig_out = triggered;
  assign irq      = frozen;

  // ------------------------------------------------------------------ host reads
  wire [3:0]  ridx  = wptr - count[3:0] + {2'd0, rbyte[5:2]};
  wire [27:0] rent  = mem[ridx];
  wire        rvld  = {1'b0, rbyte[5:2]} < count;
  reg  [7:0]  dbyte;
  always @* begin
    case (rbyte[1:0])
      2'd0:    dbyte = rent[7:0];
      2'd1:    dbyte = rent[15:8];
      2'd2:    dbyte = rent[23:16];
      default: dbyte = {4'd0, rent[27:24]};
    endcase
  end

  wire       rmon = raddr[7:4] == 4'hF;
  wire       rm   = raddr[3];
  always @* begin
    rdata = 8'd0;
    case (raddr)
      8'hE0: rdata = {5'd0, frozen, triggered, armed};
      8'hE1: rdata = {3'd0, src};
      8'hE2: rdata = {4'd0, post};
      8'hE3: rdata = {3'd0, cause};
      8'hE4: rdata = {3'd0, count};
      8'hE5: rdata = ttime[7:0];
      8'hE6: rdata = ttime[15:8];
      8'hE7: rdata = ttime[23:16];
      8'hE8: rdata = pins[7:0];
      8'hE9: rdata = {4'd0, pins[11:8]};
      8'hEA: rdata = rvld ? dbyte : 8'd0;
      8'hEC: rdata = hits[0];
      8'hED: rdata = hits[1];
      default: ;
    endcase
    if (rmon)
      case (raddr[2:0])
        3'd0: rdata = {a_sync[rm], 1'b0, edsel[rm], pin[rm]};
        3'd1: rdata = {6'd0, sidx[rm]};
        3'd2: rdata = mask[rm][7:0];
        3'd3: rdata = {4'd0, mask[rm][11:8]};
        3'd4: rdata = value[rm][7:0];
        3'd5: rdata = {4'd0, value[rm][11:8]};
        3'd6: rdata = {2'd0, cmin[rm]};
        default: rdata = {2'd0, cmax[rm]};
      endcase
  end

  // ------------------------------------------------------------------ registers
  wire       wmon = wr && waddr[7:4] == 4'hF;
  wire       wm   = waddr[3];
  integer m;
  always @(posedge clk) begin
    if (!rst_n) begin
      armed <= 1'b0; triggered <= 1'b0; frozen <= 1'b0;
      src <= 5'd0; cause <= 5'd0; post <= 4'd0; left <= 4'd0; ttime <= 24'd0;
      pins <= 12'hFFF; wptr <= 4'd0; count <= 5'd0; rbyte <= 6'd0;
      tin1 <= 1'b0; tin2 <= 1'b0; tin3 <= 1'b0; ood_prev <= 1'b0; shown_prev <= 3'd0;
      for (m = 0; m < 2; m = m + 1) begin
        pin[m] <= 4'd0; edsel[m] <= 2'd0; a_sync[m] <= 1'b0;
        sidx[m] <= 2'd0; mask[m] <= 12'd0; value[m] <= 12'd0;
        cmin[m] <= 6'd0; cmax[m] <= 6'd63; since[m] <= 16'hFFFF; hits[m] <= 8'd0;
      end
    end else begin
      // the cycle's own updates
      if (record) begin
        mem[wptr] <= {vis, cnt[15:0]};
        wptr <= wptr + 4'd1;
        if (count != 5'd16) count <= count + 5'd1;
      end
      if (fire) begin
        triggered <= 1'b1; cause <= fired; ttime <= cnt;
        if (post == 4'd0) frozen <= 1'b1;
        else              left <= post;
      end else if (triggered && !frozen && record) begin
        left <= left - 4'd1;
        if (left == 4'd1) frozen <= 1'b1;
      end
      for (m = 0; m < 2; m = m + 1) begin
        if (match[m] && hits[m] != 8'hFF) hits[m] <= hits[m] + 8'd1;
        since[m] <= pin_edge[m] ? 16'd1 : (since[m] == 16'hFFFF) ? 16'hFFFF : since[m] + 16'd1;
      end
      if (rtake && raddr == 8'hEA) rbyte <= rbyte + 6'd1;
      tin1 <= trig_pad; tin2 <= tin1; tin3 <= tin2;
      ood_prev <= ear_ood; shown_prev <= ear_shown;

      // host writes, last so they win
      if (ctrl_wr && !wdata[1]) begin
        triggered <= 1'b0; frozen <= 1'b0; armed <= wdata[0];
        if (wdata[0]) begin
          count <= 5'd0; wptr <= 4'd0; cause <= 5'd0; rbyte <= 6'd0;
        end
      end
      if (wr) begin
        case (waddr)
          8'hE1: src <= wdata[4:0];
          8'hE2: post <= wdata[3:0];
          8'hE8: pins[7:0] <= wdata;
          8'hE9: pins[11:8] <= wdata[3:0];
          8'hEA: rbyte <= 6'd0;
          8'hEC: hits[0] <= 8'd0;
          8'hED: hits[1] <= 8'd0;
          default: ;
        endcase
      end
      if (wmon)
        case (waddr[2:0])
          3'd0: begin
            pin[wm] <= wdata[3:0]; edsel[wm] <= wdata[5:4];
            a_sync[wm] <= wdata[7]; since[wm] <= 16'hFFFF;
          end
          3'd1: sidx[wm] <= wdata[1:0];
          3'd2: mask[wm][7:0] <= wdata;
          3'd3: mask[wm][11:8] <= wdata[3:0];
          3'd4: value[wm][7:0] <= wdata;
          3'd5: value[wm][11:8] <= wdata[3:0];
          3'd6: cmin[wm] <= wdata[5:0];
          default: cmax[wm] <= wdata[5:0];
        endcase
    end
  end

endmodule
