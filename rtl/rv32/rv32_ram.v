`timescale 1ns/1ps

// Word-organised memory behind the bus: an asynchronous read of the whole
// aligned word (a continuous assign, so the array never enters a sensitivity
// list) and a strobe-masked write at the accepting edge. The word index is
// the offset from BASE, the window's first address, so the memory does not
// care where the bus placed it; the bus guarantees that `addr` lies inside
// the window, so the index cannot overrun even when WORDS is not a power of
// two (the framebuffer is 19,200 words). Contents are unspecified at reset:
// the testbench zero-fills and loads the image through the hierarchy, and
// synthesis sees no initial block.
module rv32_ram #(
    parameter integer WORDS = 1048576,       // the contract's 4 MiB
    parameter [31:0] BASE = 32'h8000_0000    // must equal the bus's window base for this instance
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
    output wire        error
);
    localparam integer INDEX_BITS = $clog2(WORDS);

    reg [31:0] mem [0:WORDS-1];
    wire [31:0] offset = addr - BASE;
    wire [INDEX_BITS-1:0] index = offset[INDEX_BITS+1:2];

    assign rdata = mem[index];
    assign ready = valid; // answers in the same cycle; a slow memory is modelled by the bus hold
    assign error = 1'b0;

    always @(posedge clk) begin
        if (valid && we) begin
            if (strb[0]) mem[index][7:0] <= wdata[7:0];
            if (strb[1]) mem[index][15:8] <= wdata[15:8];
            if (strb[2]) mem[index][23:16] <= wdata[23:16];
            if (strb[3]) mem[index][31:24] <= wdata[31:24];
        end
    end

    wire unused_ok = &{1'b0, reset, offset[31:INDEX_BITS+2], offset[1:0]};
endmodule
