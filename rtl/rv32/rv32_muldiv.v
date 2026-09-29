`timescale 1ns/1ps

// The M extension's arithmetic: one 32-step iterative unit for all eight
// operations. Multiplication is shift-and-add (one multiplier bit per cycle,
// least significant first) and division is restoring division (one quotient
// bit per cycle, most significant first): the same two algorithms that
// programs/rv32/rt/muldiv.c runs in software for RV32I builds, here as one
// 64-bit shift register, a 33-bit adder for the multiply step and a 33-bit
// subtractor for the divide step.
//
// Both run on magnitudes. Each operand's sign is taken off at `start` (by its
// own signedness: mulhsu reads rs1 as signed and rs2 as unsigned) and put
// back on the result at the end, so one unsigned datapath serves all eight
// operations. The two corner cases need no special path through the loop:
// division by zero leaves an all-ones quotient and the dividend as the
// remainder, and the only signed overflow, INT32_MIN / -1, comes out as
// INT32_MIN with a zero remainder, both as the spec defines them. Only the
// quotient's sign fix is suppressed for a zero divisor, so -7 / 0 is -1.
//
// Timing is fixed: `start` loads the operands, 32 cycles step, and `valid`
// rises with the last step, so it holds from the 33rd cycle until the next
// start or reset. The core waits in MD_WAIT for it (docs/rv32-groundwork.md).
module rv32_muldiv (
    input  wire        clk,
    input  wire        reset,
    // One cycle: latch funct3, a and b and begin. They are sampled on this
    // edge only, so they need not be held stable while the unit steps. A start
    // while an operation is still stepping abandons it and begins the new one;
    // the core never does that, and the testbench stops the run if it does.
    input  wire        start,
    input  wire [2:0]  funct3,  // 0 mul, 1 mulh, 2 mulhsu, 3 mulhu, 4 div, 5 divu, 6 rem, 7 remu
    input  wire [31:0] a,       // rs1
    input  wire [31:0] b,       // rs2
    // `valid`: `result` holds the finished operation's value. Cleared by reset
    // and by start, set by the 32nd step; while it is low `result` is
    // meaningless (a stale or partial value).
    output reg         valid,
    output reg  [31:0] result
);
    reg [2:0] op;
    reg [5:0] count;        // steps left; zero when idle or finished
    reg [31:0] hi;          // the product's high half, or the partial remainder
    reg [31:0] lo;          // multiplier bits not yet used, or dividend bits becoming quotient bits
    reg [31:0] operand;     // |multiplicand| or |divisor|
    reg negate;             // the product or quotient changes sign at the end
    reg negate_remainder;   // the remainder takes the dividend's sign

    // Signedness by operation: rs1 is signed for mul/mulh/mulhsu/div/rem, rs2
    // for mul/mulh/div/rem. (mul's low half is the same either way.)
    wire is_div = funct3[2];
    wire a_signed = is_div ? !funct3[0] : (funct3[1:0] != 2'b11);
    wire b_signed = is_div ? !funct3[0] : !funct3[1];
    wire a_negative = a_signed && a[31];
    wire b_negative = b_signed && b[31];
    wire [31:0] a_magnitude = a_negative ? -a : a;
    wire [31:0] b_magnitude = b_negative ? -b : b;

    // One step. Multiply: add the multiplicand to the high half when the
    // multiplier's low bit is set, then shift the pair right; after 32 steps
    // {hi, lo} is the 64-bit product. Divide: shift {hi, lo} left one bit and
    // subtract the divisor from the top when it fits, recording a quotient
    // bit in the vacated low bit; after 32 steps lo is the quotient and hi
    // the remainder.
    wire [32:0] sum = {1'b0, hi} + (lo[0] ? {1'b0, operand} : 33'd0);
    wire [32:0] shifted = {hi, lo[31]};
    // The shifted remainder is below twice the divisor, so a 33-bit difference
    // is negative exactly when bit 32 is set: that bit is the borrow.
    wire [32:0] trial = shifted - {1'b0, operand};
    wire fits = !trial[32];

    wire busy = (count != 6'd0);  // stepping; read by the testbench's start-while-busy check

    always @(posedge clk) begin
        if (reset) begin
            count <= 6'd0;
            valid <= 1'b0;
            op <= 3'd0;
            hi <= 32'd0;
            lo <= 32'd0;
            operand <= 32'd0;
            negate <= 1'b0;
            negate_remainder <= 1'b0;
        end else if (start) begin
            op <= funct3;
            count <= 6'd32;
            valid <= 1'b0;
            hi <= 32'd0;
            lo <= is_div ? a_magnitude : b_magnitude;
            operand <= is_div ? b_magnitude : a_magnitude;
            negate <= (a_negative ^ b_negative) && !(is_div && b == 32'd0);
            negate_remainder <= a_negative;
        end else if (busy) begin
            count <= count - 6'd1;
            valid <= (count == 6'd1);
            if (op[2]) begin
                // Whichever is kept is below the divisor, so it fits 32 bits.
                hi <= fits ? trial[31:0] : shifted[31:0];
                lo <= {lo[30:0], fits};
            end else begin
                hi <= sum[32:1];
                lo <= {sum[0], lo[31:1]};
            end
        end
    end

    // Put the signs back and pick the half the operation asks for.
    wire [63:0] product = {hi, lo};
    wire [63:0] signed_product = negate ? -product : product;
    wire [31:0] quotient = negate ? -lo : lo;
    wire [31:0] remainder = negate_remainder ? -hi : hi;

    always @* begin
        case (op)
            3'd0: result = signed_product[31:0];
            3'd1, 3'd2, 3'd3: result = signed_product[63:32];
            3'd4, 3'd5: result = quotient;
            default: result = remainder;
        endcase
    end
endmodule
