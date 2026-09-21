`timescale 1ns/1ps
// Unlike x0, f0 is ordinary writable storage. Three reads support fused ops.
module rv32_fregfile (
    input wire clk, input wire reset, input wire we,
    input wire [4:0] waddr, input wire [31:0] wdata,
    input wire [4:0] raddr1, raddr2, raddr3,
    output wire [31:0] rdata1, rdata2, rdata3
);
    reg [31:0] regs [0:31];
    integer i;
    always @(posedge clk) begin
        if (reset) begin
            for (i = 0; i < 32; i = i + 1) regs[i] <= 32'd0;
        end else if (we) regs[waddr] <= wdata;
    end
    assign rdata1 = regs[raddr1];
    assign rdata2 = regs[raddr2];
    assign rdata3 = regs[raddr3];
endmodule
