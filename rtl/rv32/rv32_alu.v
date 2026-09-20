`timescale 1ns/1ps

// Pure combinational adder/subtractor. The M3 subset needs only add and
// subtract: every address, target, and result is one of the two. Later
// slices widen the operation select; the shape stays the labs/02-alu one.
module rv32_alu (
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire        sub,
    output wire [31:0] result
);
    // A - B = A + ~B + 1; the carry-in is the subtract select itself.
    assign result = a + (b ^ {32{sub}}) + {31'd0, sub};
endmodule
