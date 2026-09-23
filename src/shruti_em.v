/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * One Shruti Event Machine: ISA v1.1 (isa/isa.yaml), cycle-exact against the ISS
 * (iss/sim.py). One instruction per cycle unless it blocks.
 *
 * Cycle convention. Everything computed during cycle c is registered at the clock edge
 * that ends it. The pin drive this module requests during cycle c (drv_*) is the value
 * the pin output register holds from cycle c+1, which is how the ISS counts "SET at c
 * takes effect at c+1" and "a slot armed for t takes effect at t".
 *
 * Host-side ordering. The ISS applies host actions (TX FIFO write, RX FIFO read) before
 * the EMs execute in the same cycle. Here a host strobe in cycle c is registered and
 * seen by the EM in cycle c+1, so an RTL strobe at c corresponds to an ISS host action at
 * c+1. TX FIFO room is judged after this cycle's EM pull, as the ISS does.
 *
 * While run = 0 the EM sits in its reset state (isa.yaml): PC 0, T tracking the counter,
 * X = Y = 0, flags clear, OSR empty, ISR empty, pins push-pull, slots free. P, the CFG
 * fields and both FIFOs keep their contents so the host can set them up before starting.
 */

`default_nettype none

module shruti_em (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        run,            // 0: hold in reset state; 1: execute
    input  wire [23:0] cnt,            // timestamp counter during this cycle
    input  wire [23:0] cnt_p1,         // cnt + 1
    input  wire [11:0] vis,            // filtered pin levels this cycle
    input  wire [11:0] vis_prev,       // filtered pin levels last cycle

    input  wire [15:0] instr,          // program word at pc
    output reg  [4:0]  pc,

    input  wire [3:0]  sync_in,        // SYNC flags as this EM sees them
    output reg  [3:0]  sync_out,       // after this EM's SYNC operation

    output reg  [7:0]  drv_en,         // drive these pins from next cycle
    output reg  [7:0]  drv_lvl,
    output wire [7:0]  drv_od,         // per-pin open-drain mode for those drives

    // host side
    input  wire [7:0]  hdata,
    input  wire        p_we_lo,
    input  wire        p_we_hi,
    input  wire        cfg_out_we,     // hdata = {2'b0, AUTOPULL, OUT_MSB, OUT_N[3:0]}
    input  wire        cfg_in_we,      // hdata = {2'b0, AUTOPUSH, IN_MSB,  IN_N[3:0]}
    input  wire        flags_clr_we,   // write 1 to clear: bit1 LATE, bit2 OVF, bit3 UNF
    input  wire        tx_push,        // hdata into the TX FIFO (dropped if full)
    input  wire        rx_pop,         // drop the RX FIFO head
    output wire [7:0]  rx_head,
    output reg  [2:0]  tx_cnt,
    output reg  [2:0]  rx_cnt,
    output reg         halted,
    output wire [3:0]  flags,          // {UNF, OVF, LATE, TO}
    output reg  [15:0] P,
    output wire [5:0]  cfg_out,
    output wire [5:0]  cfg_in,
    output reg  [15:0] X,
    output reg  [15:0] Y
);

  // ---------------------------------------------------------------- state
  reg [23:0] T;
  reg        tp;                       // T_PASSED
  reg [15:0] osr, isr;
  reg [4:0]  osr_cnt, isr_cnt;
  reg [11:0] snapshot;
  reg        out_msb, autopull, in_msb, autopush;
  reg [3:0]  out_n, in_n;
  reg        f_to, f_late, f_ovf, f_unf;
  reg [7:0]  pin_mode;
  reg        blk;                      // the instruction at pc started in an earlier cycle
  reg        sampled;                  // IN: the bit is already in the ISR
  reg [1:0]  s_busy;
  reg [23:0] s_time0, s_time1;
  reg [2:0]  s_pin0, s_pin1;
  reg        s_val0, s_val1;
  reg        s_ord;                    // 1: slot 1 was armed after slot 0
  reg [7:0]  txm0, txm1, txm2, txm3;
  reg [7:0]  rxm0, rxm1, rxm2, rxm3;
  reg [1:0]  tx_rd, rx_rd;

  assign flags   = {f_unf, f_ovf, f_late, f_to};
  assign cfg_out = {autopull, out_msb, out_n};
  assign cfg_in  = {autopush, in_msb, in_n};

  // ---------------------------------------------------------------- decode
  wire [3:0]  op     = instr[15:12];
  wire [3:0]  pin4   = instr[11:8];
  wire [2:0]  w_mode = instr[7:5];
  wire [4:0]  w_tmo  = instr[4:0];
  wire [1:0]  o_val  = instr[7:6];
  wire [5:0]  d6     = instr[5:0];
  wire        i_snap = instr[7];
  wire        a_now  = instr[11];
  wire [10:0] d11    = instr[10:0];
  wire [1:0]  a_shift = instr[10:9];
  wire        sh_dir = instr[11];
  wire        sh_msb = instr[10];
  wire        sh_auto = instr[9];
  wire [3:0]  sh_n   = instr[8:5];
  wire        blockb = instr[0];
  wire [1:0]  s_tgt  = instr[11:10];
  wire [9:0]  s_imm  = instr[9:0];
  wire [3:0]  s_pin  = instr[9:6];
  wire        s_lvl  = instr[5];
  wire        s_od   = instr[4];
  wire [2:0]  j_cond = instr[11:9];
  wire [3:0]  j_sel  = instr[8:5];
  wire [4:0]  j_tgt  = instr[4:0];
  wire        y_op   = instr[2];
  wire [1:0]  y_flag = instr[1:0];

  // ---------------------------------------------------------------- time arithmetic
  // diffT = T - cnt. While T_PASSED is clear, T is in the future, so diffT is in [1, 2^23).
  wire [23:0] diffT  = T - cnt;
  wire        tp_eff = tp | (diffT == 24'd0);

  wire [15:0] p_shr  = P >> a_shift;
  reg  [23:0] addend;
  always @* begin
    case (op)
      4'd1:    addend = (w_tmo == 5'd0 || w_tmo > 5'd23) ? 24'd0 : (24'd1 << w_tmo);
      4'd4:    addend = {13'd0, d11};
      4'd5:    addend = {8'd0, p_shr};
      default: addend = {18'd0, d6};
    endcase
  end
  wire [23:0] tgt     = T + addend;          // T + d, T + 2^n or T + delta
  wire [23:0] dtgt    = diffT + addend;      // tgt - cnt
  wire        eq_tgt  = (dtgt == 24'd0);
  wire        fut_tgt = !eq_tgt && !dtgt[23];
  wire [23:0] cnt_add = cnt + addend;        // ADDT now candidate

  // ---------------------------------------------------------------- FIFO views
  reg [7:0] tx_head, tx_second;
  always @* begin
    case (tx_rd)
      2'd0: begin tx_head = txm0; tx_second = txm1; end
      2'd1: begin tx_head = txm1; tx_second = txm2; end
      2'd2: begin tx_head = txm2; tx_second = txm3; end
      default: begin tx_head = txm3; tx_second = txm0; end
    endcase
  end
  reg [7:0] rx_head_r;
  always @* begin
    case (rx_rd)
      2'd0: rx_head_r = rxm0;
      2'd1: rx_head_r = rxm1;
      2'd2: rx_head_r = rxm2;
      default: rx_head_r = rxm3;
    endcase
  end
  assign rx_head = rx_head_r;

  wire        out_two   = out_n[3];                   // word longer than 8 bits
  wire        in_two    = in_n[3];
  wire        can_pull  = out_two ? (tx_cnt >= 3'd2) : (tx_cnt != 3'd0);
  wire        can_push  = in_two ? (rx_cnt <= 3'd2) : (rx_cnt <= 3'd3);
  wire [15:0] pull_word = out_two ? {tx_second, tx_head} : {8'd0, tx_head};
  wire [4:0]  out_w     = {1'b0, out_n} + 5'd1;
  wire [4:0]  in_w      = {1'b0, in_n} + 5'd1;
  wire        osr_empty = (osr_cnt >= out_w);

  // ---------------------------------------------------------------- execute
  reg        done, jump, halt_now;
  reg [4:0]  jtarget;
  reg        t_we;
  reg [23:0] t_val;
  reg        tp_val;
  reg        pull_now, push_now;
  reg [15:0] push_word;
  reg        arm, arm_now;
  reg [2:0]  arm_pin;
  reg        arm_lvl;
  reg        set_now;
  reg [2:0]  set_pin;
  reg        set_lvl;

  reg [15:0] n_osr, n_isr, n_X, n_Y, n_P;
  reg [4:0]  n_osr_cnt, n_isr_cnt;
  reg [11:0] n_snap;
  reg        n_out_msb, n_autopull, n_in_msb, n_autopush;
  reg [3:0]  n_out_n, n_in_n;
  reg        n_to, n_late, n_ovf, n_unf;
  reg [7:0]  n_pin_mode;
  reg        n_sampled;

  // scratch
  reg        need_pull, empty_e, bitv, lvl, match, to_now, do_sample, sbit, sampled_e;
  reg [15:0] osr_e, isr_e;
  reg [4:0]  osr_cnt_e, isr_cnt_e;
  reg [3:0]  bidx;
  reg        take;

  always @* begin
    done = 1'b0; jump = 1'b0; halt_now = 1'b0; jtarget = j_tgt;
    t_we = 1'b0; t_val = T; tp_val = 1'b1;
    pull_now = 1'b0; push_now = 1'b0; push_word = isr;
    arm = 1'b0; arm_now = 1'b0; arm_pin = pin4[2:0]; arm_lvl = 1'b0;
    set_now = 1'b0; set_pin = s_pin[2:0]; set_lvl = s_lvl;
    n_osr = osr; n_isr = isr; n_X = X; n_Y = Y; n_P = P;
    n_osr_cnt = osr_cnt; n_isr_cnt = isr_cnt; n_snap = snapshot;
    n_out_msb = out_msb; n_autopull = autopull; n_in_msb = in_msb; n_autopush = autopush;
    n_out_n = out_n; n_in_n = in_n;
    n_to = f_to; n_late = f_late; n_ovf = f_ovf; n_unf = f_unf;
    n_pin_mode = pin_mode; n_sampled = sampled;
    sync_out = sync_in;
    need_pull = 1'b0; empty_e = 1'b0; bitv = 1'b0; lvl = 1'b0; match = 1'b0; to_now = 1'b0;
    do_sample = 1'b0; sbit = 1'b0; sampled_e = 1'b0;
    osr_e = osr; isr_e = isr; osr_cnt_e = osr_cnt; isr_cnt_e = isr_cnt; bidx = 4'd0;
    take = 1'b0;

    if (run && !halted) begin
      case (op)
        // ------------------------------------------------ WAIT
        4'd1: begin
          if (pin4 > 4'd11 || w_mode > 3'd4 || w_tmo > 5'd23) begin
            halt_now = 1'b1;
          end else begin
            case (w_mode)
              3'd0: match = !vis_prev[pin4] &&  vis[pin4];
              3'd1: match =  vis_prev[pin4] && !vis[pin4];
              3'd2: match =  vis_prev[pin4] ^   vis[pin4];
              3'd3: match =  vis[pin4];
              default: match = !vis[pin4];
            endcase
            to_now = (w_tmo != 5'd0) && (blk ? eq_tgt : !fut_tgt);
            if (match) begin
              t_we = 1'b1; t_val = cnt; tp_val = 1'b1;
              n_snap = vis;
              done = 1'b1;
            end else if (to_now) begin
              n_to = 1'b1;
              t_we = 1'b1; t_val = tgt; tp_val = 1'b1;
              done = 1'b1;
            end
          end
        end
        // ------------------------------------------------ OUT
        4'd2: begin
          if (pin4 > 4'd7) begin
            halt_now = 1'b1;
          end else begin
            need_pull = (o_val == 2'd2) && osr_empty && autopull;
            if (!(need_pull && !can_pull)) begin
              if (need_pull) begin
                pull_now = 1'b1;
                osr_e = pull_word;
                osr_cnt_e = 5'd0;
              end
              n_osr = osr_e;
              n_osr_cnt = osr_cnt_e;
              if (s_busy != 2'b11) begin
                empty_e = (osr_cnt_e >= out_w);
                bidx = out_msb ? (out_n - osr_cnt_e[3:0]) : osr_cnt_e[3:0];
                bitv = osr_e[bidx];
                if (!o_val[1]) begin
                  lvl = o_val[0];
                end else if (empty_e) begin
                  n_unf = 1'b1;
                  lvl = o_val[0];                    // OSR: 0, ~OSR: 1
                end else begin
                  lvl = o_val[0] ? !bitv : bitv;
                  if (!o_val[0]) n_osr_cnt = osr_cnt_e + 5'd1;
                end
                arm_lvl = lvl;
                if (fut_tgt && dtgt != 24'd1) begin
                  arm = 1'b1;
                end else begin
                  if (!fut_tgt) n_late = 1'b1;
                  arm_now = 1'b1;                    // takes effect next cycle
                end
                done = 1'b1;
              end
            end
          end
        end
        // ------------------------------------------------ IN
        4'd3: begin
          if (pin4 > 4'd11) begin
            halt_now = 1'b1;
          end else begin
            if (!sampled) begin
              if (i_snap) begin
                do_sample = 1'b1;
              end else if (!blk) begin
                if (eq_tgt) do_sample = 1'b1;
                else if (!fut_tgt) begin
                  n_late = 1'b1;
                  do_sample = 1'b1;
                end
              end else if (eq_tgt) begin
                do_sample = 1'b1;
              end
            end
            sbit = i_snap ? snapshot[pin4] : vis[pin4];
            if (do_sample && isr_cnt < in_w) begin
              bidx = in_msb ? (in_n - isr_cnt[3:0]) : isr_cnt[3:0];
              isr_e = isr | ({15'd0, sbit} << bidx);
              isr_cnt_e = isr_cnt + 5'd1;
            end
            n_isr = isr_e;
            n_isr_cnt = isr_cnt_e;
            sampled_e = sampled | do_sample;
            if (do_sample) n_sampled = 1'b1;
            if (sampled_e) begin
              if (autopush && isr_cnt_e >= in_w) begin
                if (can_push) begin
                  push_now = 1'b1;
                  push_word = isr_e;
                  n_isr = 16'd0;
                  n_isr_cnt = 5'd0;
                  done = 1'b1;
                end
              end else begin
                done = 1'b1;
              end
            end
          end
        end
        // ------------------------------------------------ ADDT, ADDTP
        4'd4, 4'd5: begin
          t_we = 1'b1;
          if (!a_now) begin
            t_val = tgt; tp_val = !fut_tgt;
          end else if (tp_eff) begin
            t_val = cnt_add; tp_val = (addend == 24'd0);
          end else if (diffT >= addend) begin
            t_val = T; tp_val = 1'b0;               // T is already later than now + delta
          end else begin
            t_val = cnt_add; tp_val = 1'b0;
          end
          done = 1'b1;
        end
        // ------------------------------------------------ SHIFT
        4'd6: begin
          if (!sh_dir) begin
            n_out_msb = sh_msb; n_autopull = sh_auto; n_out_n = sh_n;
            n_osr_cnt = {1'b0, sh_n} + 5'd1;
          end else begin
            n_in_msb = sh_msb; n_autopush = sh_auto; n_in_n = sh_n;
            n_isr = 16'd0; n_isr_cnt = 5'd0;
          end
          done = 1'b1;
        end
        // ------------------------------------------------ PUSH
        4'd7: begin
          if (can_push) begin
            push_now = 1'b1; push_word = isr;
            n_isr = 16'd0; n_isr_cnt = 5'd0;
            done = 1'b1;
          end else if (!blockb) begin
            n_ovf = 1'b1;
            n_isr = 16'd0; n_isr_cnt = 5'd0;
            done = 1'b1;
          end
        end
        // ------------------------------------------------ PULL
        4'd8: begin
          if (can_pull) begin
            pull_now = 1'b1;
            n_osr = pull_word; n_osr_cnt = 5'd0;
            done = 1'b1;
          end else if (!blockb) begin
            done = 1'b1;
          end
        end
        // ------------------------------------------------ SET
        4'd9: begin
          case (s_tgt)
            2'd0: begin
              if (s_pin > 4'd7) begin
                halt_now = 1'b1;
              end else begin
                n_pin_mode[s_pin[2:0]] = s_od;
                set_now = 1'b1;
                done = 1'b1;
              end
            end
            2'd1: begin n_X = {6'd0, s_imm}; done = 1'b1; end
            2'd2: begin n_Y = {6'd0, s_imm}; done = 1'b1; end
            default: begin n_P = {6'd0, s_imm}; done = 1'b1; end
          endcase
        end
        // ------------------------------------------------ JMP
        4'd10: begin
          case (j_cond)
            3'd0: begin
              case (j_sel)
                4'd0: begin take = 1'b1; done = 1'b1; end
                4'd1: begin take = f_to; n_to = 1'b0; done = 1'b1; end
                4'd2: begin take = f_late; done = 1'b1; end
                4'd3: begin take = (tx_cnt == 3'd0); done = 1'b1; end
                4'd4: begin take = !osr_empty; done = 1'b1; end
                default: halt_now = 1'b1;
              endcase
            end
            3'd1: begin
              if (X != 16'd0) begin n_X = X - 16'd1; take = 1'b1; end
              done = 1'b1;
            end
            3'd2: begin
              if (Y != 16'd0) begin n_Y = Y - 16'd1; take = 1'b1; end
              done = 1'b1;
            end
            3'd3, 3'd4: begin
              if (j_sel > 4'd11) begin
                halt_now = 1'b1;
              end else begin
                take = (vis[j_sel] == (j_cond == 3'd3));
                done = 1'b1;
              end
            end
            default: halt_now = 1'b1;
          endcase
          jump = take;
        end
        // ------------------------------------------------ SYNC
        4'd11: begin
          if (!y_op) begin
            sync_out[y_flag] = 1'b1;
            done = 1'b1;
          end else if (sync_in[y_flag]) begin
            sync_out[y_flag] = 1'b0;
            done = 1'b1;
          end
        end
        // ------------------------------------------------ HALT and opcodes 12-15
        default: halt_now = 1'b1;
      endcase
    end
  end

  // ---------------------------------------------------------------- pin drive for next cycle
  // Per EM, in increasing priority: the earlier-armed slot firing, the later-armed slot
  // firing, then this cycle's instruction (SET, or an OUT that takes effect next cycle).
  wire fire0 = s_busy[0] && (s_time0 == cnt_p1);
  wire fire1 = s_busy[1] && (s_time1 == cnt_p1);
  assign drv_od = n_pin_mode;

  always @* begin
    drv_en = 8'd0;
    drv_lvl = 8'd0;
    if (run) begin
      if (s_ord) begin
        if (fire0) begin drv_en[s_pin0] = 1'b1; drv_lvl[s_pin0] = s_val0; end
        if (fire1) begin drv_en[s_pin1] = 1'b1; drv_lvl[s_pin1] = s_val1; end
      end else begin
        if (fire1) begin drv_en[s_pin1] = 1'b1; drv_lvl[s_pin1] = s_val1; end
        if (fire0) begin drv_en[s_pin0] = 1'b1; drv_lvl[s_pin0] = s_val0; end
      end
      if (arm_now) begin drv_en[arm_pin] = 1'b1; drv_lvl[arm_pin] = arm_lvl; end
      if (set_now) begin drv_en[set_pin] = 1'b1; drv_lvl[set_pin] = set_lvl; end
    end
  end

  // ---------------------------------------------------------------- FIFO bookkeeping
  wire [2:0] tx_pop_n  = !pull_now ? 3'd0 : (out_two ? 3'd2 : 3'd1);
  wire [2:0] tx_after  = tx_cnt - tx_pop_n;
  wire       tx_accept = tx_push && (tx_after != 3'd4);
  wire [1:0] tx_wr     = tx_rd + tx_cnt[1:0];
  wire [2:0] rx_push_n = !push_now ? 3'd0 : (in_two ? 3'd2 : 3'd1);
  wire       rx_take   = rx_pop && (rx_cnt != 3'd0);
  wire [1:0] rx_wr     = rx_rd + rx_cnt[1:0];
  wire [1:0] rx_wr1    = rx_wr + 2'd1;

  // ---------------------------------------------------------------- registers
  always @(posedge clk) begin
    if (!rst_n) begin
      P <= 16'd0;
      out_msb <= 1'b0; autopull <= 1'b0; out_n <= 4'd7;
      in_msb <= 1'b0; autopush <= 1'b0; in_n <= 4'd7;
      tx_cnt <= 3'd0; tx_rd <= 2'd0;
      rx_cnt <= 3'd0; rx_rd <= 2'd0;
    end else begin
      // P and CFG: host writes, SET P and SHIFT
      if (run) begin
        P <= n_P;
        out_msb <= n_out_msb; autopull <= n_autopull; out_n <= n_out_n;
        in_msb <= n_in_msb; autopush <= n_autopush; in_n <= n_in_n;
      end
      if (p_we_lo) P[7:0] <= hdata;
      if (p_we_hi) P[15:8] <= hdata;
      if (cfg_out_we) begin out_n <= hdata[3:0]; out_msb <= hdata[4]; autopull <= hdata[5]; end
      if (cfg_in_we)  begin in_n  <= hdata[3:0]; in_msb  <= hdata[4]; autopush <= hdata[5]; end

      // TX FIFO: the EM pulls, the host pushes
      tx_rd  <= tx_rd + tx_pop_n[1:0];
      tx_cnt <= tx_after + {2'd0, tx_accept};
      if (tx_accept) begin
        case (tx_wr)
          2'd0: txm0 <= hdata;
          2'd1: txm1 <= hdata;
          2'd2: txm2 <= hdata;
          default: txm3 <= hdata;
        endcase
      end
      // RX FIFO: the EM pushes, the host pops
      if (rx_push_n != 3'd0) begin
        case (rx_wr)
          2'd0: rxm0 <= push_word[7:0];
          2'd1: rxm1 <= push_word[7:0];
          2'd2: rxm2 <= push_word[7:0];
          default: rxm3 <= push_word[7:0];
        endcase
      end
      if (rx_push_n == 3'd2) begin
        case (rx_wr1)
          2'd0: rxm0 <= push_word[15:8];
          2'd1: rxm1 <= push_word[15:8];
          2'd2: rxm2 <= push_word[15:8];
          default: rxm3 <= push_word[15:8];
        endcase
      end
      rx_rd  <= rx_rd + {1'b0, rx_take};
      rx_cnt <= rx_cnt + rx_push_n - {2'd0, rx_take};
    end
  end

  always @(posedge clk) begin
    if (!rst_n || !run) begin
      pc <= 5'd0;
      T <= cnt_p1;                 // so T == counter in the first cycle of a run
      tp <= 1'b1;
      X <= 16'd0; Y <= 16'd0;
      osr <= 16'd0; osr_cnt <= {1'b0, out_n} + 5'd1;
      isr <= 16'd0; isr_cnt <= 5'd0;
      snapshot <= 12'd0;
      f_to <= 1'b0; f_late <= 1'b0; f_ovf <= 1'b0; f_unf <= 1'b0;
      pin_mode <= 8'd0;
      halted <= 1'b0;
      blk <= 1'b0; sampled <= 1'b0;
      s_busy <= 2'b00; s_ord <= 1'b0;
      s_time0 <= 24'd0; s_time1 <= 24'd0;
      s_pin0 <= 3'd0; s_pin1 <= 3'd0; s_val0 <= 1'b0; s_val1 <= 1'b0;
    end else begin
      if (t_we) begin
        T <= t_val;
        tp <= tp_val;
      end else begin
        tp <= tp_eff;
      end
      X <= n_X; Y <= n_Y;
      osr <= n_osr; osr_cnt <= n_osr_cnt;
      isr <= n_isr; isr_cnt <= n_isr_cnt;
      snapshot <= n_snap;
      f_to <= n_to;
      f_late <= n_late && !(flags_clr_we && hdata[1]);
      f_ovf  <= n_ovf  && !(flags_clr_we && hdata[2]);
      f_unf  <= n_unf  && !(flags_clr_we && hdata[3]);
      pin_mode <= n_pin_mode;
      if (halt_now) halted <= 1'b1;
      if (done) begin
        pc <= jump ? jtarget : pc + 5'd1;
        blk <= 1'b0;
        sampled <= 1'b0;
      end else if (!halted && !halt_now) begin
        blk <= 1'b1;
        sampled <= n_sampled;
      end
      // scheduler slots: a slot is free again in the cycle it fires
      if (fire0) s_busy[0] <= 1'b0;
      if (fire1) s_busy[1] <= 1'b0;
      if (arm) begin
        if (!s_busy[0]) begin
          s_busy[0] <= 1'b1; s_time0 <= tgt; s_pin0 <= arm_pin; s_val0 <= arm_lvl; s_ord <= 1'b0;
        end else begin
          s_busy[1] <= 1'b1; s_time1 <= tgt; s_pin1 <= arm_pin; s_val1 <= arm_lvl; s_ord <= 1'b1;
        end
      end
    end
  end

endmodule
