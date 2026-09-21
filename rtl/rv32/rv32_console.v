`timescale 1ns/1ps

// Debug console (docs/rv32.md, "Console"): a byte store to +0 transmits the
// byte, a byte read of +5 (strobe 0010 on the word at +4) returns the
// always-ready status 0x20 in lane 1; every other offset, width, or
// direction inside the eight-byte window is refused with `error`.
// BUSY_CYCLES > 0 holds `ready` low for that many cycles before each byte is
// accepted, the contract's permission for a device to wait; status reads and
// refused accesses are never delayed. `tx_valid` is a one-cycle strobe in
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
    output wire [7:0]  tx_byte
);
    localparam [31:0] BUSY = BUSY_CYCLES;

    wire tx_ok = we && (addr[2:0] == 3'd0) && (strb == 4'b0001);
    wire status_ok = !we && (addr[2:0] == 3'd5) && (strb == 4'b0010);
    reg [31:0] waited; // cycles the presented byte has waited; never exceeds BUSY

    assign ready = valid && (!tx_ok || waited == BUSY);
    assign error = we ? !tx_ok : !status_ok;
    assign rdata = status_ok ? 32'h0000_2000 : 32'd0;
    assign tx_valid = valid && ready && tx_ok;
    assign tx_byte = wdata[7:0];

    // Restarts whenever no request is presented or one is accepted.
    always @(posedge clk) begin
        if (reset || !valid || ready)
            waited <= 32'd0;
        else
            waited <= waited + 32'd1;
    end

    wire unused_ok = &{1'b0, addr[31:3], wdata[31:8]};
endmodule
