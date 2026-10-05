`timescale 1ns/1ps

// Debug console (docs/rv32.md, "Console"): the subset of a 16550 that Linux's
// 8250 driver drives (issue #36), with no interrupt. Every access is one byte,
// on its own lane (strobe 0001 << addr[1:0]); any other width, and a store to
// LSR or MSR, is refused with `error`.
//   +0 RBR/THR: a store transmits; a read (O2) returns the waiting byte, or 0
//      when there is none, and `rx_take` tells the host it was taken. While
//      LCR.DLAB is set, +0 and +1 are the divisor latch instead, stored and
//      read back, and nothing is sent or taken.
//   +1 IER, four bits.   +2 IIR on reads, FCR on writes (ignored).
//   +3 LCR.   +4 MCR, five bits.   +5 LSR: 0x60 (THRE and TEMT, the
//      transmitter is always empty) and bit 0 (DR) while a byte waits.
//   +6 MSR: 0xb0, a connected line.   +7 SCR.
// IIR follows a 16550's priorities among the enabled sources: 0x04 for a
// waiting byte when IER.RDI is set, else 0x02 when IER.THRI is set, else 0x01.
// BUSY_CYCLES > 0 holds `ready` low for that many cycles before each byte is
// accepted, the contract's permission for a device to wait; other accesses
// and refused ones are never delayed. `tx_valid` is a one-cycle strobe in
// the accepting cycle, so the host takes the byte exactly once.
module rv32_console #(
    parameter integer BUSY_CYCLES = 0
) (
    input  wire        clk,
    input  wire        reset,
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [3:0]  strb,
    input  wire [31:0] wdata,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error,
    output wire        tx_valid,
    output wire [7:0]  tx_byte,
    input  wire        rx_valid,  // the host has a byte waiting
    input  wire [7:0]  rx_byte,
    output wire        rx_take    // the guest took it in this cycle
);
    localparam [31:0] BUSY = BUSY_CYCLES;
    localparam [2:0] RBR = 3'd0, IER = 3'd1, IIR = 3'd2, LCR = 3'd3, MCR = 3'd4, LSR = 3'd5, MSR = 3'd6, SCR = 3'd7;
    // The values, named as tools/rv32emu_core.h names them.
    localparam [7:0] IIR_RDI = 8'h04, IIR_THRI = 8'h02, IIR_NONE = 8'h01;
    localparam [7:0] LSR_TX_IDLE = 8'h60;  // THRE and TEMT: the transmitter is always empty
    localparam [7:0] MSR_LINE = 8'hb0;     // DCD, DSR and CTS: a connected line
    localparam IER_RDI = 0, IER_THRI = 1;

    reg [3:0] ier;
    reg [7:0] lcr, scr, dll, dlm;
    reg [4:0] mcr;
    wire [2:0] off = addr[2:0];
    wire byte_ok = strb == (4'b0001 << addr[1:0]);
    wire dlab = lcr[7];
    wire [7:0] wbyte = wdata[8 * addr[1:0] +: 8]; // the strobed lane carries the byte

    wire rbr_thr = byte_ok && off == RBR && !dlab; // under DLAB, +0 is the divisor latch
    wire tx_ok = we && rbr_thr;
    wire rbr_ok = !we && rbr_thr;
    reg [31:0] waited; // cycles the presented byte has waited; never exceeds BUSY

    reg [7:0] rbyte;
    always @(*) begin
        case (off)
            RBR: rbyte = dlab ? dll : (rx_valid ? rx_byte : 8'd0);
            IER: rbyte = dlab ? dlm : {4'd0, ier};
            IIR: rbyte = (ier[IER_RDI] && rx_valid) ? IIR_RDI : ier[IER_THRI] ? IIR_THRI : IIR_NONE;
            LCR: rbyte = lcr;
            MCR: rbyte = {3'd0, mcr};
            LSR: rbyte = LSR_TX_IDLE | {7'd0, rx_valid};
            MSR: rbyte = MSR_LINE;
            default: rbyte = scr; // SCR, the eighth offset
        endcase
    end

    assign ready = valid && (!tx_ok || waited == BUSY);
    assign error = !byte_ok || (we && (off == LSR || off == MSR));
    assign rdata = {24'd0, rbyte} << {addr[1:0], 3'd0};
    assign rx_take = valid && ready && rbr_ok && rx_valid;
    assign tx_valid = valid && ready && tx_ok;
    assign tx_byte = wbyte;

    always @(posedge clk) begin
        if (reset) begin
            ier <= 4'd0;
            lcr <= 8'd0;
            mcr <= 5'd0;
            scr <= 8'd0;
            dll <= 8'd0;
            dlm <= 8'd0;
        end else if (valid && ready && we && !error) begin // once, in the accepting cycle
            case (off)
                RBR: if (dlab) dll <= wbyte;
                IER: if (dlab) dlm <= wbyte; else ier <= wbyte[3:0];
                LCR: lcr <= wbyte;
                MCR: mcr <= wbyte[4:0];
                SCR: scr <= wbyte;
                IIR: ;     // FCR: there is no FIFO to enable or clear
                default: ; // THR (transmitted, not stored); LSR and MSR are refused
            endcase
        end
    end

    // Restarts whenever no request is presented or one is accepted.
    always @(posedge clk) begin
        if (reset || !valid || ready)
            waited <= 32'd0;
        else
            waited <= waited + 32'd1;
    end

    wire unused_ok = &{1'b0, addr[31:3]};
endmodule
