`timescale 1ns/1ps

// Thirty-one 32-bit registers plus the constant x0. Two combinational read
// ports (muxes over the registers), one write port at the clock edge. Reset
// clears every register so both backends agree on a read before a write.
module rv32_regfile (
    input  wire        clk,
    input  wire        reset,
    input  wire        we,
    input  wire [4:0]  waddr,
    input  wire [31:0] wdata,
    input  wire [4:0]  raddr1,
    input  wire [4:0]  raddr2,
    output wire [31:0] rdata1,
    output wire [31:0] rdata2
);
    reg [31:0] regs [1:31];
    integer i;

    // A write to x0 is discarded here; the datapath never tests for x0 again
    // (only the retirement port repeats the test, to suppress the trace field).
    always @(posedge clk) begin
        if (reset) begin
            for (i = 1; i < 32; i = i + 1)
                regs[i] <= 32'd0;
        end else if (we && waddr != 5'd0) begin
            regs[waddr] <= wdata;
        end
    end

    assign rdata1 = (raddr1 == 5'd0) ? 32'd0 : regs[raddr1];
    assign rdata2 = (raddr2 == 5'd0) ? 32'd0 : regs[raddr2];
endmodule
