`timescale 1ns/1ps

// Combinational ALU for RV32I: the eight funct3 operations with `alt`
// selecting subtract (funct3 0) or arithmetic shift (funct3 5), plus the
// three comparison flags every branch and set-less-than shares. One
// subtractor produces the difference, the unsigned borrow, and the signed
// order; the shifts are a barrel shifter on the low five bits of `b`.
module rv32_alu (
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [2:0]  op,     // funct3
    input  wire        alt,    // funct7[5]: sub for op 0, sra for op 5
    output reg  [31:0] result,
    output wire        eq,
    output wire        lt,     // signed a < b
    output wire        ltu     // unsigned a < b
);
    localparam [2:0] OP_ADD = 3'd0, OP_SLL = 3'd1, OP_SLT = 3'd2, OP_SLTU = 3'd3,
                     OP_XOR = 3'd4, OP_SRL = 3'd5, OP_OR = 3'd6; // 3'd7 is and, the default arm

    // A - B = A + ~B + 1 with a 33rd bit: bit 32 is the borrow, which is
    // the unsigned comparison; the signed one is the sign of the difference
    // unless the operand signs differ, in which case the negative one is smaller.
    wire [32:0] diff = {1'b0, a} - {1'b0, b};
    assign ltu = diff[32];
    assign lt = (a[31] != b[31]) ? a[31] : diff[31];
    assign eq = (diff[31:0] == 32'd0);

    wire [4:0] shamt = b[4:0];

    always @* begin
        case (op)
            OP_ADD:  result = alt ? diff[31:0] : a + b;
            OP_SLL:  result = a << shamt;
            OP_SLT:  result = {31'd0, lt};
            OP_SLTU: result = {31'd0, ltu};
            OP_XOR:  result = a ^ b;
            OP_SRL:  result = alt ? $unsigned($signed(a) >>> shamt) : a >> shamt;
            OP_OR:   result = a | b;
            default: result = a & b; // and
        endcase
    end
endmodule
