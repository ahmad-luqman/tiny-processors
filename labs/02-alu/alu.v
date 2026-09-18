`timescale 1ns/1ps

// Pure combinational logic: no clock, reset, or stored flags.
module alu (
    input  wire [7:0] a,
    input  wire [7:0] b,
    input  wire [2:0] op,
    output reg  [7:0] result,
    output wire      zero,
    output wire      negative,
    output reg       carry,
    output reg       overflow
);
    localparam [2:0] OP_ADD = 3'd0,
                     OP_SUB = 3'd1,
                     OP_AND = 3'd2,
                     OP_OR  = 3'd3,
                     OP_XOR = 3'd4,
                     OP_NOT = 3'd5,
                     OP_SHL = 3'd6,
                     OP_SHR = 3'd7;

    always @* begin
        // Assign every output on every path, avoiding latches and stale flags.
        result = 8'd0;
        carry = 1'b0;
        overflow = 1'b0;
        case (op)
            OP_ADD: begin
                // Zero-extension keeps the ninth (carry) bit of the sum.
                {carry, result} = {1'b0, a} + {1'b0, b};
                overflow = ~(a[7] ^ b[7]) & (result[7] ^ a[7]);
            end
            OP_SUB: begin
                // A - B = A + ~B + 1. Carry-out means NO borrow.
                {carry, result} = {1'b0, a} + {1'b0, ~b} + 9'd1;
                overflow = (a[7] ^ b[7]) & (result[7] ^ a[7]);
            end
            OP_AND: result = a & b;
            OP_OR:  result = a | b;
            OP_XOR: result = a ^ b;
            OP_NOT: result = ~a;
            OP_SHL: begin
                result = {a[6:0], 1'b0};
                carry = a[7];
            end
            OP_SHR: begin
                result = {1'b0, a[7:1]};
                carry = a[0];
            end
            default: begin end // Defaults above cover simulation X/Z opcodes.
        endcase
    end

    assign zero = (result == 8'd0);
    assign negative = result[7];
endmodule
