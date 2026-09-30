`timescale 1ns/1ps

// DMA window (docs/rv32.md, "DMA window"; issue #20): START at +0 and END at +4
// are read/write words, and [START, END) is the RAM G1's blit source and G2's
// depth buffer must lie in. The engines test it when they validate a job, so a
// write never affects a job already running. It sits in its own page, outside
// the span the kernel grants a program that drives the engines, so only the
// kernel sets it. At reset it is all of RAM, which leaves the engines' existing
// RAM-bounds checks the only limit. Byte and halfword accesses are refused here;
// the bus decodes only these 8 bytes, so it refuses every other offset.
module rv32_dma_window #(
    parameter integer RAM_WORDS = 1048576
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
    output reg  [31:0] window_start,
    output reg  [31:0] window_end
);
    localparam [31:0] RAM_BASE = 32'h8000_0000;
    localparam [31:0] RAM_END = RAM_BASE + RAM_WORDS * 4;

    wire word = (strb == 4'b1111);

    assign ready = valid;
    assign error = !word;
    assign rdata = addr[2] ? window_end : window_start;

    always @(posedge clk) begin
        if (reset) begin
            window_start <= RAM_BASE;
            window_end <= RAM_END;
        end else if (valid && we && word) begin
            if (addr[2])
                window_end <= wdata;
            else
                window_start <= wdata;
        end
    end

    wire unused_ok = &{1'b0, addr[31:3], addr[1:0]};
endmodule
