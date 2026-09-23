/*
 * Copyright (c) 2026 Gautam Ramachandra
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host SPI slave, mode 0, MSB first, in the core clock domain.
 *
 * SCK, MOSI and CS_n are synchronised (two flops) and SCK edges are detected in the core
 * clock domain, so SCK must stay below core clock / 8 (6.25 MHz at 50 MHz).
 *
 * Transaction: CS_n low, a command byte (bit 7 = 1 for read, 0 for write; bits 6..0 must
 * be 0), an address byte, then any number of data bytes. The address increments after
 * each data byte unless it is a FIFO port (the caller says which, through `port`).
 * Writes are presented as a one-cycle strobe (wr_stb, wr_addr, wr_data) when a data byte
 * completes. For reads, rd_addr is the address whose data (rd_data) will be shifted out
 * next; the byte is captured before the first SCK edge of that byte, and rd_take pulses
 * when the host clocks its first bit (so a FIFO read port pops only bytes actually read).
 * MISO changes shortly after the SCK rising edge, which a mode 0 master samples on the
 * next rising edge.
 */

`default_nettype none

module shruti_spi (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       sck,
    input  wire       mosi,
    input  wire       cs_n,
    output wire       miso,
    output reg        wr_stb,
    output reg  [7:0] wr_addr,
    output reg  [7:0] wr_data,
    output wire [7:0] rd_addr,
    input  wire [7:0] rd_data,
    output reg        rd_take,
    input  wire       port          // rd_addr / current address is a FIFO port
);

  reg [2:0] sck_s;         // [2] is the previous synchronised level, for edge detection
  reg [1:0] mosi_s, cs_s;
  always @(posedge clk) begin
    if (!rst_n) begin
      sck_s <= 3'b000; mosi_s <= 2'b00; cs_s <= 2'b11;
    end else begin
      sck_s <= {sck_s[1:0], sck};
      mosi_s <= {mosi_s[0], mosi};
      cs_s <= {cs_s[0], cs_n};
    end
  end
  wire active = !cs_s[1];
  wire rise   = sck_s[1] && !sck_s[2];
  wire bit_in = mosi_s[1];

  reg [2:0] bitn;          // bits received in the current byte
  reg [6:0] sh;            // bits received so far
  reg [1:0] phase;         // 0 command, 1 address, 2 data
  reg       rd;            // read transaction
  reg [7:0] addr;
  reg [7:0] miso_sh;
  reg       load;          // capture rd_data for the next byte on the next cycle
  reg       take_pend;     // pulse rd_take on the first SCK edge of the loaded byte

  assign miso    = miso_sh[7];
  assign rd_addr = addr;

  wire [7:0] byte_in = {sh, bit_in};

  always @(posedge clk) begin
    if (!rst_n) begin
      bitn <= 3'd0; sh <= 7'd0; phase <= 2'd0; rd <= 1'b0; addr <= 8'd0;
      miso_sh <= 8'd0; load <= 1'b0; take_pend <= 1'b0;
      wr_stb <= 1'b0; wr_addr <= 8'd0; wr_data <= 8'd0; rd_take <= 1'b0;
    end else begin
      wr_stb <= 1'b0;
      rd_take <= 1'b0;
      if (!active) begin
        bitn <= 3'd0; phase <= 2'd0; rd <= 1'b0; load <= 1'b0; take_pend <= 1'b0;
        miso_sh <= 8'd0;
      end else begin
        if (load) begin
          load <= 1'b0;
          miso_sh <= rd_data;
          take_pend <= 1'b1;
        end
        if (rise) begin
          bitn <= bitn + 3'd1;
          sh <= byte_in[6:0];
          if (!load) miso_sh <= {miso_sh[6:0], 1'b0};
          if (take_pend) begin
            rd_take <= 1'b1;
            take_pend <= 1'b0;
          end
          if (bitn == 3'd7) begin
            case (phase)
              2'd0: begin
                rd <= byte_in[7];
                phase <= 2'd1;
              end
              2'd1: begin
                addr <= byte_in;
                phase <= 2'd2;
                if (rd) load <= 1'b1;
              end
              default: begin
                if (!rd) begin
                  wr_stb <= 1'b1;
                  wr_addr <= addr;
                  wr_data <= byte_in;
                end
                if (!port) addr <= addr + 8'd1;
                if (rd) load <= 1'b1;
              end
            endcase
          end
        end
      end
    end
  end

endmodule
