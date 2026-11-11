// Formal harnesses for the Ear's classifier datapath (verify/ear.sby). Each drives the RTL's
// own units (shruti_ear_mac, shruti_ear_argmax in src/shruti_ear.v) with symbolic values
// fixed for the whole run (anyconst) and checks the result against the specification
// (ear/SPEC.md) written independently below: the inputs of the dot products change every
// step (anyseq), and the specification's running sum is a ghost register in exact arithmetic,
// proved equal by k-induction; the argmax run is a finite sequence from its first cycle, so a bounded
// check one step past its end covers every state it reaches.

`default_nettype none

// Layer 1: one hidden unit, 72 six-bit features, 72 ternary weights and a signed 8-bit
// threshold. A ghost register keeps the specification's running sum, B + sum(w_i * f_i),
// in exact 20-bit arithmetic. The RTL's 16-bit running sum equals it after every term (so
// no step overflows), stays within 128 + 63 n after n terms, and the requantised value is
// min(255, max(0, sum) >> SHIFT) at every step, so in particular at the unit's last term.
module ear_layer1_dot (input wire clk);
  (* anyconst *) reg signed [7:0] b;
  (* anyconst *) reg [3:0] sh;
  (* anyseq *)   reg [5:0] fx;                // the features, one per step
  (* anyseq *)   reg [1:0] wc;                // the weight codes

  reg [6:0] n = 7'd0;
  reg signed [15:0] acc = 16'sd0;             // the start value is never read
  reg signed [19:0] g = 20'sd0;               // ghost: the specification's running sum
  wire signed [15:0] base = (n == 7'd0) ? {{8{b[7]}}, b} : acc;
  wire signed [19:0] gbase = (n == 7'd0) ? {{12{b[7]}}, b} : g;
  wire signed [19:0] wx = (wc == 2'b01) ? $signed({14'd0, fx}) :
                          (wc == 2'b11) ? -$signed({14'd0, fx}) : 20'sd0;
  wire signed [19:0] gnext = gbase + wx;
  wire signed [15:0] sum;
  wire [7:0] hval;
  shruti_ear_mac u (.base(base), .xin({10'd0, fx}), .wcode(wc), .shift(sh),
                    .sum(sum), .hval(hval));

  always @(posedge clk) if (n < 7'd72) begin acc <= sum; g <= gnext; n <= n + 7'd1; end

  wire signed [19:0] bound = 20'sd128 + 20'sd63 * $signed({13'd0, n});
  wire signed [19:0] req0  = gnext < 0 ? 20'sd0 : gnext >>> sh;
  wire [7:0] req = req0 > 20'sd255 ? 8'd255 : req0[7:0];
  always @* begin
    assert (n <= 7'd72);
    if (n > 7'd0) begin
      assert ($signed({{4{acc[15]}}, acc}) == g);                      // L1: equals the spec
      assert (g <= bound && g >= -bound);                              // L2: its range
    end
    if (n < 7'd72) begin
      assert ($signed({{4{sum[15]}}, sum}) == gnext);                  // L3: exact, no overflow
      assert (hval == req);                                            // L4: requantisation
    end
  end
endmodule

// Layer 2: one output, 8 hidden units of 8 bits and 8 ternary weights, from 0. The sum
// equals the specification's sum(w_j * h_j) and lies within +-255 n after n terms, so the
// output is within +-2,040 (8 x 255), the range the argmax may assume.
module ear_layer2_dot (input wire clk);
  (* anyseq *) reg [7:0] hx;
  (* anyseq *) reg [1:0] wc;
  reg [3:0] n = 4'd0;
  reg signed [15:0] acc = 16'sd0;
  reg signed [19:0] g = 20'sd0;
  wire signed [15:0] base = (n == 4'd0) ? 16'sd0 : acc;
  wire signed [19:0] gbase = (n == 4'd0) ? 20'sd0 : g;
  wire signed [19:0] wx = (wc == 2'b01) ? $signed({12'd0, hx}) :
                          (wc == 2'b11) ? -$signed({12'd0, hx}) : 20'sd0;
  wire signed [19:0] gnext = gbase + wx;
  wire signed [15:0] sum;
  wire [7:0] hval;
  shruti_ear_mac u (.base(base), .xin({8'd0, hx}), .wcode(wc), .shift(4'd0),
                    .sum(sum), .hval(hval));
  always @(posedge clk) if (n < 4'd8) begin acc <= sum; g <= gnext; n <= n + 4'd1; end

  wire signed [19:0] bound = 20'sd255 * $signed({16'd0, n});
  always @* begin
    assert (n <= 4'd8);
    if (n > 4'd0) begin
      assert ($signed({{4{acc[15]}}, acc}) == g);                      // O1: equals the spec
      assert (g <= bound && g >= -bound);                              // O2: within 255 n
    end
    if (n < 4'd8) assert ($signed({{4{sum[15]}}, sum}) == gnext);      // exact, no overflow
  end
endmodule

// Argmax and decision: 8 outputs within +-2,040 (O2) arrive in class order 0..7 into
// registers that start anywhere. Then the best is the maximum, its class the lowest class
// with that maximum, the second the maximum over the other classes, the confident flag is
// best - second >= MARGIN[class], and OOD is best < FLOOR or edges < MIN_EDGES.
module ear_argmax_spec (input wire clk);
  (* anyconst *) reg [127:0] O;
  (* anyconst *) reg [63:0] M;
  (* anyconst *) reg [11:0] FL;
  (* anyconst *) reg [8:0] NE, ME;
  (* anyseq *)   reg signed [15:0] best0, second0;
  (* anyseq *)   reg [2:0] bestk0;

  reg [3:0] n = 4'd0;
  reg signed [15:0] best = 16'sd0, second = 16'sd0;   // overridden at n = 0 by best0...
  reg [2:0] bestk = 3'd0;
  wire signed [15:0] o = O[16 * n[2:0] +: 16];
  wire signed [15:0] n_best, n_sec;
  wire [2:0] n_bestk;
  wire w_conf, w_ood;
  shruti_ear_argmax u (.o_r(o), .o_k(n[2:0]), .best(n == 4'd0 ? best0 : best),
                       .second(n == 4'd0 ? second0 : second), .bestk(n == 4'd0 ? bestk0 : bestk),
                       .n_best(n_best), .n_sec(n_sec), .n_bestk(n_bestk),
                       .margin(M), .floor_(FL), .nedges(NE), .min_edges(ME),
                       .w_conf(w_conf), .w_ood(w_ood));
  always @(posedge clk) if (n < 4'd8) begin
    best <= n_best; second <= n_sec; bestk <= n_bestk; n <= n + 4'd1;
  end

  integer k;
  always @* for (k = 0; k < 8; k = k + 1)
    assume ($signed(O[16 * k +: 16]) >= -2040 && $signed(O[16 * k +: 16]) <= 2040);

  integer mx, mk, sc, fl;
  always @* begin
    mx = -40000; mk = 0;
    for (k = 0; k < 8; k = k + 1)
      if ($signed(O[16 * k +: 16]) > mx) begin mx = $signed(O[16 * k +: 16]); mk = k; end
    sc = -40000;
    for (k = 0; k < 8; k = k + 1)
      if (k != mk && $signed(O[16 * k +: 16]) > sc) sc = $signed(O[16 * k +: 16]);
    fl = $signed(FL);
  end
  always @* if (n == 4'd8) begin
    assert (best == mx);                                               // A1: the maximum
    assert (bestk == mk);                                              // A2: lowest class
    assert (second == sc);                                             // A3: the runner-up
    assert (w_conf == (mx - sc >= M[8 * mk +: 8]));                    // A4: confident
    assert (w_ood == (mx < fl || NE < ME));                            // A5: OOD
  end
endmodule
