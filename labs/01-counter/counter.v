`timescale 1ns/1ps

// Eight flip-flops plus an incrementer, with synchronous reset and enable.
module counter (
    input  wire       clk,
    input  wire       reset,
    input  wire       enable,
    output reg  [7:0] count
);
    always @(posedge clk) begin
        if (reset)
            count <= 8'd0;
        else if (enable)
            count <= count + 8'd1;
        // No assignment means the flip-flops retain their previous value.
    end
endmodule
