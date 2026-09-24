/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * The Ear: feature extractor and ternary classifier, bit-exact to ear/features.py and
 * ear/mlp.py as defined in ear/SPEC.md.
 *
 * Window. While enabled, the Ear counts filtered edges on 4 host-selected pins. A window
 * closes at the end of the cycle in which its edge count reaches 256, or after 65,536
 * cycles. Then it pauses for exactly 648 cycles, counting nothing, and the next window
 * starts with every counter cleared. The first window starts in the first cycle the enable
 * bit is set.
 *
 * Pause schedule (648 cycles, p = 0..647):
 *   p = 0..6    four restoring dividers, one per pin, give floor(64 * high / length)
 *   p = 7       the pins are ranked by (edge feature, time-high feature), largest first,
 *               ties by pin number (canonical order)
 *   p = 8..647  one ternary weight per cycle into one 16-bit accumulator: 576 for layer 1,
 *               64 for layer 2, with the argmax folded into layer 2 as each output completes
 * The class, confidence and out-of-distribution flags change at the end of p = 647.
 *
 * With HOLD set, the Ear stops after the pause with the window's counters intact, so the
 * host can read the 72 features through the FEATURE port. Clearing HOLD clears the counters
 * in the cycle the write takes effect, and the new window starts in the cycle after.
 *
 * Weights: 640 ternary weights in a 1,280-bit ring that turns by one weight per multiply
 * cycle and is back in place when inference ends. The host loads it through the WEIGHT
 * port while the Ear is disabled and not pausing (STATUS bit 2 clear), 160 bytes in chain
 * order; the 8 thresholds are registers. Clearing the enable during a pause lets the pause
 * finish first.
 */

`default_nettype none

module shruti_ear (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [11:0] vis,          // filtered pin levels this cycle
    input  wire [11:0] vis_prev,     // and last cycle
    // host
    input  wire        wr,
    input  wire [7:0]  waddr,
    input  wire [7:0]  wdata,
    input  wire [7:0]  raddr,
    input  wire        rtake,        // the host clocked the first bit of the byte at raddr
    output reg  [7:0]  rdata,
    // results
    output reg  [2:0]  cls,
    output reg         conf,
    output reg         ood
);

  // ------------------------------------------------------------------ host configuration
  reg        en, hold;
  reg [15:0] sel;                    // four 4-bit pin selects
  reg [3:0]  shift;
  reg [7:0]  margin;
  reg [11:0] floor_;
  reg [8:0]  min_edges;
  reg [63:0] thr;                    // B[0..7], signed bytes

  // ------------------------------------------------------------------ window state
  localparam S_RUN = 2'd0, S_PAUSE = 2'd1, S_HELD = 2'd2;
  reg [1:0]  state;
  reg [9:0]  p;                      // pause cycle
  reg [16:0] wlen;                   // cycles of this window before the current one;
                                     // during the pause, the window's length
  reg [8:0]  nedges;                 // edges this window (saturates at 511)
  reg [7:0]  nwin;                   // windows classified (wraps)
  reg        valid;

  wire counting = en && state == S_RUN;
  wire clear_now = (state == S_PAUSE) ? (p == 10'd647 && (!en || !hold))
                                      : (!en || (state == S_HELD && !hold));

  // per pin
  reg [47:0] hist   [0:3];           // 8 bins x 6 bits, bin k in bits 6k+5:6k
  reg [4:0]  min_h  [0:3];
  reg        has_min[0:3];
  reg [5:0]  min_c  [0:3];
  reg [5:0]  max_c  [0:3];
  reg [7:0]  nedg   [0:3];
  reg [16:0] high   [0:3];
  reg [15:0] since  [0:3];
  reg        has_last[0:3];
  reg [17:0] rem    [0:3];           // divider remainder
  reg [6:0]  quo    [0:3];
  // per ordered pair, index 4a + b (diagonal unused)
  reg [5:0]  near   [0:15];
  reg [5:0]  hi     [0:15];

  // canonical order: slot s holds pin ord[2s+1:2s]
  reg [7:0]  ord;

  // ------------------------------------------------------------------ pin selection
  reg  [3:0] lvl, edg;
  integer q;
  always @* begin
    for (q = 0; q < 4; q = q + 1) begin
      if (sel[4*q +: 4] < 4'd12) begin
        lvl[q] = vis[sel[4*q +: 4]];
        edg[q] = vis[sel[4*q +: 4]] ^ vis_prev[sel[4*q +: 4]];
      end else begin
        lvl[q] = 1'b0;
        edg[q] = 1'b0;
      end
    end
  end

  // ------------------------------------------------------------------ interval update
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

  function [5:0] sat_add6(input [5:0] a, input [5:0] b);
    reg [6:0] s;
    begin
      s = {1'b0, a} + {1'b0, b};
      sat_add6 = s[6] ? 6'd63 : s[5:0];
    end
  endfunction

  reg [47:0] n_hist [0:3];
  reg [4:0]  n_min_h[0:3];
  reg [5:0]  n_min_c[0:3];
  reg [5:0]  n_max_c[0:3];
  reg [3:0]  n_has_min;

  // Values derived from each pin's interval counter and minimum, registered a cycle ahead so
  // the histogram update starts from flip-flops: the code of `since`, whether its half-octave
  // is below the minimum and by how much (clamped to 8), its distance above the minimum, and
  // the compares against the shortest and longest codes. pd_*[pn] always equals the function
  // of this cycle's since, min_h, min_c and max_c (see the next-state copies below).
  reg [5:0]  pd_code [0:3];
  reg        pd_lt   [0:3];          // h < min_h
  reg [3:0]  pd_sc   [0:3];          // min_h - h, clamped to 8
  reg [4:0]  pd_dd   [0:3];          // h - min_h
  reg        pd_maxr [0:3];          // (max_c >> 1) - h > 6: the longest leaves the burst
  reg        pd_clt  [0:3];          // code < min_c
  reg        pd_cgt  [0:3];          // code > max_c

  // Shifting the histogram when the minimum drops by s half-octaves: bin j < 7 takes bin
  // j - s (0 if j < s), and bin 7 takes the saturated sum of bins 7 - s .. 7. The suffix
  // sums suf[k] = min(63, bins k..7) give that sum for every s at once.
  reg [5:0]  code;
  reg [4:0]  h, dd;
  reg [3:0]  sc;
  reg [2:0]  bin;
  reg [47:0] sh;
  reg [5:0]  suf [0:7];
  integer pn, k;
  always @* begin
    k = 0;
    for (pn = 0; pn < 4; pn = pn + 1) begin
      n_hist[pn] = hist[pn];
      n_min_h[pn] = min_h[pn];
      n_min_c[pn] = min_c[pn];
      n_max_c[pn] = max_c[pn];
      n_has_min[pn] = has_min[pn];
      code = pd_code[pn];
      h = code[5:1];
      sc = pd_sc[pn];
      suf[7] = hist[pn][42 +: 6];
      for (k = 6; k >= 0; k = k - 1)
        suf[k] = sat_add6(suf[k + 1], hist[pn][6*k +: 6]);
      sh = hist[pn];
      dd = 5'd0; bin = 3'd0;
      if (counting && edg[pn] && has_last[pn]) begin
        if (!has_min[pn]) begin
          n_has_min[pn] = 1'b1;
          n_min_h[pn] = h;
          n_min_c[pn] = code;
          n_max_c[pn] = code;
          n_hist[pn] = 48'd1;
        end else begin
          if (pd_lt[pn]) begin
            for (k = 0; k < 7; k = k + 1)
              sh[6*k +: 6] = (sc > k[3:0]) ? 6'd0 : hist[pn][6*(k - sc) +: 6];
            sh[42 +: 6] = suf[(sc >= 4'd7) ? 0 : 7 - sc];
            n_min_h[pn] = h;
            if (pd_maxr[pn]) n_max_c[pn] = code;
            dd = 5'd0;
          end else begin
            dd = pd_dd[pn];
            if (pd_dd[pn] <= 5'd6 && pd_cgt[pn]) n_max_c[pn] = code;
          end
          bin = (dd > 5'd7) ? 3'd7 : dd[2:0];
          sh[6*bin +: 6] = sat_add6(sh[6*bin +: 6], 6'd1);
          n_hist[pn] = sh;
          if (pd_clt[pn]) n_min_c[pn] = code;
        end
      end
    end
  end

  // next-cycle copies of since, min_h, min_c and max_c, and the derived values from them
  reg [15:0] nx_since;
  reg [4:0]  nx_min_h, nx_h;
  reg [5:0]  nx_min_c, nx_max_c, nx_code;
  reg [5:0]  n_pd_code [0:3];
  reg        n_pd_lt [0:3], n_pd_maxr [0:3], n_pd_clt [0:3], n_pd_cgt [0:3];
  reg [3:0]  n_pd_sc [0:3];
  reg [4:0]  n_pd_dd [0:3];
  reg [4:0]  nx_s;
  integer pq;
  always @* begin
    for (pq = 0; pq < 4; pq = pq + 1) begin
      if (clear_now) begin
        nx_since = 16'd0; nx_min_h = 5'd0; nx_min_c = 6'd63; nx_max_c = 6'd0;
      end else if (counting) begin
        nx_since = edg[pq] ? 16'd1 : (since[pq] == 16'hFFFF) ? 16'hFFFF : since[pq] + 16'd1;
        nx_min_h = n_min_h[pq]; nx_min_c = n_min_c[pq]; nx_max_c = n_max_c[pq];
      end else begin
        nx_since = since[pq]; nx_min_h = min_h[pq]; nx_min_c = min_c[pq]; nx_max_c = max_c[pq];
      end
      nx_code = log2_code(nx_since);
      nx_h = nx_code[5:1];
      nx_s = nx_min_h - nx_h;
      n_pd_code[pq] = nx_code;
      n_pd_lt[pq] = nx_h < nx_min_h;
      n_pd_sc[pq] = (nx_s > 5'd8) ? 4'd8 : nx_s[3:0];
      n_pd_dd[pq] = nx_h - nx_min_h;
      n_pd_maxr[pq] = {1'b0, nx_max_c[5:1]} - {1'b0, nx_h} > 6'd6;
      n_pd_clt[pq] = nx_code < nx_min_c;
      n_pd_cgt[pq] = nx_code > nx_max_c;
    end
  end

  // edges this cycle and the closing condition
  wire [2:0] nnow   = {2'd0, edg[0]} + {2'd0, edg[1]} + {2'd0, edg[2]} + {2'd0, edg[3]};
  wire [9:0] ecount = {1'b0, nedges} + {7'd0, nnow};
  wire       close  = counting && (ecount >= 10'd256 || wlen == 17'h0FFFF);

  // ------------------------------------------------------------------ canonical rank (p = 7)
  wire [5:0] fe   [0:3];             // edge feature: saturated 8-bit count, top 6 bits
  wire [5:0] fh   [0:3];             // time-high feature
  genvar g;
  generate
    for (g = 0; g < 4; g = g + 1) begin : g_feat
      assign fe[g] = nedg[g][7:2];
      assign fh[g] = (quo[g] > 7'd63) ? 6'd63 : quo[g][5:0];
    end
  endgenerate

  reg [7:0] n_ord;
  reg [1:0] rank;
  integer a, b;
  always @* begin
    n_ord = 8'd0;
    for (a = 0; a < 4; a = a + 1) begin
      rank = 2'd0;
      for (b = 0; b < 4; b = b + 1)
        if (b != a && ({fe[b], fh[b]} > {fe[a], fh[a]} || ({fe[b], fh[b]} == {fe[a], fh[a]} && b < a)))
          rank = rank + 2'd1;
      n_ord[2*rank +: 2] = a[1:0];
    end
  end

  // ------------------------------------------------------------------ feature read
  // Feature index as (fs, ft): fs 0..3 pin slot with ft 0..11 its feature, fs 4 near and
  // fs 5 hi with ft the canonical pair (0,1) (0,2) (0,3) (1,0) ... (3,2).
  reg  [2:0] mfs, hfs;               // multiply and host counters
  reg  [3:0] mft, hft;
  wire       mac = state == S_PAUSE && p >= 10'd8;
  wire [2:0] fs = mac ? mfs : hfs;
  wire [3:0] ft = mac ? mft : hft;

  reg [5:0] feat;
  reg [1:0] pin, ca, cb, pa, pb;
  reg [3:0] pr;
  always @* begin
    pin = ord[2*fs[1:0] +: 2];
    ca = ft[3:0] / 3;                // pair index -> canonical slots (a, b)
    pr = ft - {ca, 1'b0} - {2'b0, ca};
    cb = (pr[1:0] >= ca) ? pr[1:0] + 2'd1 : pr[1:0];
    pa = ord[2*ca +: 2];
    pb = ord[2*cb +: 2];
    if (fs < 3'd4) begin
      case (ft)
        4'd8:    feat = min_c[pin];
        4'd9:    feat = max_c[pin];
        4'd10:   feat = fe[pin];
        4'd11:   feat = fh[pin];
        default: feat = hist[pin][6*ft[2:0] +: 6];
      endcase
    end else if (fs == 3'd4) begin
      feat = near[{pa, pb}];
    end else begin
      feat = hi[{pa, pb}];
    end
  end

  // ------------------------------------------------------------------ multiply-accumulate
  reg [1279:0] chain;
  reg [63:0]   hid;                  // hidden units, 8 bits each
  reg [2:0]    mj, mk, mjj;          // layer 1 unit; layer 2 output and input
  reg          layer2;
  reg signed [15:0] acc, best, second;
  reg [2:0]    bestk;

  wire [1:0] wcode = chain[1:0];
  wire signed [15:0] xin = layer2 ? {8'd0, hid[8*mjj +: 8]} : {10'd0, feat};
  wire first = layer2 ? (mjj == 3'd0) : (mfs == 3'd0 && mft == 4'd0);
  wire last  = layer2 ? (mjj == 3'd7) : (mfs == 3'd5 && mft == 4'd11);
  wire signed [15:0] base = first ? (layer2 ? 16'sd0 : {{8{thr[8*mj + 7]}}, thr[8*mj +: 8]}) : acc;
  wire signed [15:0] term = (wcode == 2'b01) ? xin : (wcode == 2'b11) ? -xin : 16'sd0;
  wire signed [15:0] sum  = base + term;
  wire [15:0] relu = sum[15] ? 16'd0 : (sum >>> shift);
  wire [7:0]  hval = (relu > 16'd255) ? 8'd255 : relu[7:0];

  // argmax as each layer-2 output completes (lowest k wins ties)
  wire               k0      = mk == 3'd0;
  wire               better  = k0 || sum > best;
  wire signed [15:0] n_best  = better ? sum : best;
  wire signed [15:0] n_sec   = k0 ? -16'sd32768 : better ? best : (sum > second ? sum : second);
  wire [2:0]         n_bestk = better ? mk : bestk;
  wire signed [15:0] gap     = n_best - n_sec;
  wire signed [15:0] floor_x = {{4{floor_[11]}}, floor_};

  // ------------------------------------------------------------------ weight loading
  reg [7:0] ldbyte;
  reg [2:0] ldn;                     // weights still to shift in from ldbyte

  // ------------------------------------------------------------------ host reads
  wire wr_ear = wr && waddr[7:4] == 4'hB;
  always @* begin
    case (raddr)
      8'hB0:   rdata = {4'd0, state == S_HELD, state == S_PAUSE, hold, en};
      8'hB1:   rdata = sel[7:0];
      8'hB2:   rdata = sel[15:8];
      8'hB3:   rdata = {4'd0, shift};
      8'hB4:   rdata = margin;
      8'hB5:   rdata = floor_[7:0];
      8'hB6:   rdata = {4'd0, floor_[11:8]};
      8'hB7:   rdata = min_edges[7:0];
      8'hB8:   rdata = {7'd0, min_edges[8]};
      8'hBA:   rdata = {valid, 2'd0, ood, conf, cls};
      8'hBB:   rdata = best[7:0];
      8'hBC:   rdata = best[15:8];
      8'hBD:   rdata = nwin;
      8'hBE:   rdata = {2'd0, feat};
      8'hBF:   rdata = nedges[8] ? 8'hFF : nedges[7:0];
      8'hC0, 8'hC1, 8'hC2, 8'hC3, 8'hC4, 8'hC5, 8'hC6, 8'hC7:
               rdata = thr[8*raddr[2:0] +: 8];
      default: rdata = 8'd0;
    endcase
  end

  // ------------------------------------------------------------------ registers
  integer r, sa, sb;
  always @(posedge clk) begin
    if (!rst_n) begin
      en <= 1'b0; hold <= 1'b0; sel <= 16'h3210; shift <= 4'd4; margin <= 8'd8;
      floor_ <= 12'd0; min_edges <= 9'd24; thr <= 64'd0;
      state <= S_RUN; p <= 10'd0; wlen <= 17'd0; nedges <= 9'd0; nwin <= 8'd0; valid <= 1'b0;
      cls <= 3'd0; conf <= 1'b0; ood <= 1'b0;
      ord <= 8'b11_10_01_00;
      hfs <= 3'd0; hft <= 4'd0; ldn <= 3'd0; ldbyte <= 8'd0;
      mfs <= 3'd0; mft <= 4'd0; mj <= 3'd0; mk <= 3'd0; mjj <= 3'd0; layer2 <= 1'b0;
      acc <= 16'sd0; best <= 16'sd0; second <= 16'sd0; bestk <= 3'd0; hid <= 64'd0;
      for (r = 0; r < 4; r = r + 1) begin
        hist[r] <= 48'd0; min_h[r] <= 5'd0; has_min[r] <= 1'b0; min_c[r] <= 6'd63;
        max_c[r] <= 6'd0; nedg[r] <= 8'd0; high[r] <= 17'd0; since[r] <= 16'd0;
        has_last[r] <= 1'b0; rem[r] <= 18'd0; quo[r] <= 7'd0;
        pd_code[r] <= 6'd0; pd_lt[r] <= 1'b0; pd_sc[r] <= 4'd0; pd_dd[r] <= 5'd0;
        pd_maxr[r] <= 1'b0; pd_clt[r] <= 1'b1; pd_cgt[r] <= 1'b0;
      end
      for (r = 0; r < 16; r = r + 1) begin
        near[r] <= 6'd0; hi[r] <= 6'd0;
      end
    end else begin
      // host writes
      if (wr_ear) begin
        case (waddr)
          8'hB0: begin en <= wdata[0]; hold <= wdata[1]; end
          8'hB1: sel[7:0] <= wdata;
          8'hB2: sel[15:8] <= wdata;
          8'hB3: shift <= wdata[3:0];
          8'hB4: margin <= wdata;
          8'hB5: floor_[7:0] <= wdata;
          8'hB6: floor_[11:8] <= wdata[3:0];
          8'hB7: min_edges[7:0] <= wdata;
          8'hB8: min_edges[8] <= wdata[0];
          8'hB9: if (!en && state != S_PAUSE) begin ldbyte <= wdata; ldn <= 3'd4; end
          8'hBE: begin hfs <= 3'd0; hft <= 4'd0; end
          default: ;
        endcase
      end
      if (wr && waddr[7:3] == 5'b11000) thr[8*waddr[2:0] +: 8] <= wdata;
      if (rtake && raddr == 8'hBE) begin
        hft <= (hft == 4'd11) ? 4'd0 : hft + 4'd1;
        if (hft == 4'd11) hfs <= (hfs == 3'd5) ? 3'd0 : hfs + 3'd1;
      end

      // window. A pause always runs to its end, so the weight ring comes back into place.
      for (r = 0; r < 4; r = r + 1) begin
        pd_code[r] <= n_pd_code[r]; pd_lt[r] <= n_pd_lt[r]; pd_sc[r] <= n_pd_sc[r];
        pd_dd[r] <= n_pd_dd[r]; pd_maxr[r] <= n_pd_maxr[r]; pd_clt[r] <= n_pd_clt[r];
        pd_cgt[r] <= n_pd_cgt[r];
      end
      if (clear_now) begin
        // cleared: the next window starts in the next cycle the Ear is enabled
        state <= S_RUN; wlen <= 17'd0; nedges <= 9'd0;
        for (r = 0; r < 4; r = r + 1) begin
          hist[r] <= 48'd0; min_h[r] <= 5'd0; has_min[r] <= 1'b0; min_c[r] <= 6'd63;
          max_c[r] <= 6'd0; nedg[r] <= 8'd0; high[r] <= 17'd0; since[r] <= 16'd0;
          has_last[r] <= 1'b0;
        end
        for (r = 0; r < 16; r = r + 1) begin
          near[r] <= 6'd0; hi[r] <= 6'd0;
        end
      end else if (state == S_PAUSE && p == 10'd647) begin
        state <= S_HELD;
      end else if (counting) begin
        wlen <= wlen + 17'd1;           // counts the closing cycle too: length in the pause
        nedges <= ecount[9] ? 9'h1FF : ecount[8:0];
        for (r = 0; r < 4; r = r + 1) begin
          hist[r] <= n_hist[r]; min_h[r] <= n_min_h[r]; has_min[r] <= n_has_min[r];
          min_c[r] <= n_min_c[r]; max_c[r] <= n_max_c[r];
          high[r] <= high[r] + {16'd0, lvl[r]};
          if (edg[r]) begin
            nedg[r] <= (nedg[r] == 8'hFF) ? 8'hFF : nedg[r] + 8'd1;
            since[r] <= 16'd1;
            has_last[r] <= 1'b1;
          end else begin
            since[r] <= (since[r] == 16'hFFFF) ? 16'hFFFF : since[r] + 16'd1;
          end
        end
        for (sa = 0; sa < 4; sa = sa + 1)
          for (sb = 0; sb < 4; sb = sb + 1)
            if (sa != sb && edg[sb]) begin
              if (edg[sa] || (has_last[sa] && since[sa] <= 16'd4))
                near[4*sa + sb] <= sat_add6(near[4*sa + sb], 6'd1);
              if (lvl[sa])
                hi[4*sa + sb] <= sat_add6(hi[4*sa + sb], 6'd1);
            end
        if (close) begin
          state <= S_PAUSE;
          p <= 10'd0;
        end
      end

      // pause: divide, rank, multiply
      if (state == S_PAUSE) begin
        p <= p + 10'd1;
        if (p <= 10'd6) begin
          for (r = 0; r < 4; r = r + 1) begin
            if (((p == 10'd0) ? {1'b0, high[r]} : rem[r]) >= {1'b0, wlen}) begin
              rem[r] <= (((p == 10'd0) ? {1'b0, high[r]} : rem[r]) - {1'b0, wlen}) << 1;
              quo[r] <= {((p == 10'd0) ? 6'd0 : quo[r][5:0]), 1'b1};
            end else begin
              rem[r] <= ((p == 10'd0) ? {1'b0, high[r]} : rem[r]) << 1;
              quo[r] <= {((p == 10'd0) ? 6'd0 : quo[r][5:0]), 1'b0};
            end
          end
        end
        if (p == 10'd7) begin
          ord <= n_ord;
          mfs <= 3'd0; mft <= 4'd0; mj <= 3'd0; mk <= 3'd0; mjj <= 3'd0; layer2 <= 1'b0;
        end
        if (mac) begin
          chain <= {chain[1:0], chain[1279:2]};
          acc <= sum;
          if (!layer2) begin
            if (last) begin
              hid[8*mj +: 8] <= hval;
              mfs <= 3'd0; mft <= 4'd0;
              mj <= mj + 3'd1;
              if (mj == 3'd7) layer2 <= 1'b1;
            end else if (mft == 4'd11) begin
              mft <= 4'd0; mfs <= mfs + 3'd1;
            end else begin
              mft <= mft + 4'd1;
            end
          end else begin
            mjj <= mjj + 3'd1;
            if (last) begin
              best <= n_best; second <= n_sec; bestk <= n_bestk;
              mk <= mk + 3'd1;
              if (mk == 3'd7) begin
                cls <= n_bestk;
                conf <= gap >= $signed({8'd0, margin});
                ood <= n_best < floor_x || nedges < min_edges;
                valid <= 1'b1;
                nwin <= nwin + 8'd1;
              end
            end
          end
        end
      end

      // weight loading, two bits per cycle, only while disabled and not in a pause
      if (ldn != 3'd0 && !en && state != S_PAUSE) begin
        chain <= {ldbyte[1:0], chain[1279:2]};
        ldbyte <= {2'd0, ldbyte[7:2]};
        ldn <= ldn - 3'd1;
      end
    end
  end

endmodule
