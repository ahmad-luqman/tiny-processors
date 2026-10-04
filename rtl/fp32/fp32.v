`timescale 1ns/1ps

// Standalone multicycle binary32 hardware. Contract and bit weights: docs/fp32.md.
// Add, multiply and FMA align the smaller operand to the larger one in an
// 80-bit window with a sticky jam, then normalize with a leading-zero count:
// a few cycles each, rounded once to the same result as the exact sum would
// give (see docs/fp32.md).
module fp32 (
    input wire clk, input wire reset,
    input wire req_valid, output wire req_ready,
    input wire [4:0] op, input wire [2:0] rm,
    input wire [31:0] a, b, c,
    output wire resp_valid, input wire resp_ready,
    output reg [31:0] result, output reg [4:0] flags, output reg error
);
    `include "fp32_states.vh"
    `include "fp32_ops.vh"
    localparam [2:0] RM_RNE=3'd0,
        RM_RTZ=3'd1,
        RM_RDN=3'd2,
        RM_RUP=3'd3,
        RM_RMM=3'd4;
    localparam [4:0] FLAG_NX=5'h1,
        FLAG_UF=5'h2,
        FLAG_OF=5'h4,
        FLAG_DZ=5'h8,
        FLAG_NV=5'h10;
    // Window geometry. Bit TOP weighs 2^exponent. An addition operand's 48-bit
    // magnitude loads at [TOP-1:OPERAND_LSB], leaving bit TOP for the carry;
    // OPERAND_LSB is also the largest exponent gap that loses no bits.
    localparam integer WINDOW = 80, TOP = WINDOW-1, SHIFT_BITS = 7;
    localparam integer OPERAND_LSB = TOP-48, SIG_LSB = WINDOW-24, INT_LSB = WINDOW-32;
    localparam [SHIFT_BITS-1:0] FULL_SHIFT = WINDOW[SHIFT_BITS-1:0];
    reg [$clog2(STATE_COUNT)-1:0] state;
    reg [4:0] operation;
    reg [2:0] mode;
    reg [31:0] x, y, z;
    // For addition, magnitude holds the trailing operand (the only one ALIGN
    // shifts) and addend the leading one; COMBINE leaves the sum in magnitude,
    // which every other operation also rounds from.
    reg [TOP:0] magnitude, addend;
    reg sign_result, sign_addend;
    reg tiny_after_rounding;
    reg [SHIFT_BITS-1:0] align_shift;
    reg signed [11:0] exponent;
    reg [5:0] iteration;
    reg [25:0] quotient;
    reg [26:0] div_remainder;
    reg [23:0] divisor;
    reg [53:0] radicand;
    reg [53:0] sqrt_remainder;
    reg [26:0] root;

    assign req_ready = state == IDLE && !reset;
    assign resp_valid = state == RESPONSE && !reset;

    // The unused_ prefix marks deliberately ignored portions of helper inputs
    // (sign, or fraction for exponent extraction) for Verilator's unused-bit
    // check. The remaining bits are used; no datapath lint category is disabled.
    function is_nan;
        input [31:0] unused_v;
        begin is_nan = &unused_v[30:23] && |unused_v[22:0]; end
    endfunction
    function is_snan;
        input [31:0] v;
        begin is_snan = is_nan(v) && !v[22]; end
    endfunction
    function is_inf;
        input [31:0] unused_v;
        begin is_inf = unused_v[30:0] == 31'h7f800000; end
    endfunction
    function is_zero;
        input [31:0] unused_v;
        begin is_zero = unused_v[30:0] == 0; end
    endfunction
    function [23:0] significand;
        input [31:0] unused_v;
        begin significand = {|unused_v[30:23], unused_v[22:0]}; end
    endfunction
    function [9:0] effective_exp;
        input [31:0] unused_v;
        begin effective_exp = unused_v[30:23] == 0 ? 10'd1 : {2'b0,unused_v[30:23]}; end
    endfunction
    // Normalize a finite significand. Signed unbiased exponent occupies [35:24].
    function [35:0] unpack;
        input [31:0] v;
        reg [23:0] s;
        reg signed [11:0] e;
        integer i;
        begin
            s = significand(v);
            e = $signed({2'b0,effective_exp(v)}) - 12'sd127;
            for (i=0; i<23; i=i+1) begin
                if (!s[23] && s != 0) begin s=s<<1; e=e-12'sd1; end
            end
            unpack = {e,s};
        end
    endfunction
    function increment;
        input [2:0] rounding;
        input negative, odd, guard_bit, sticky;
        begin
            case (rounding)
                RM_RNE: increment = guard_bit && (sticky || odd);
                RM_RTZ: increment = 0;
                RM_RDN: increment = negative && (guard_bit || sticky);
                RM_RUP: increment = !negative && (guard_bit || sticky);
                RM_RMM: increment = guard_bit;
                default: increment = 0;
            endcase
        end
    endfunction
    // Right shift with every shifted-out bit ORed into bit 0 (sticky jam).
    // The full-shift branch gives what the general one would, but Yosys
    // builds a smaller circuit with it, so keep it.
    function [TOP:0] jam_right;
        input [TOP:0] v;
        input [SHIFT_BITS-1:0] amount;
        begin
            if (amount >= FULL_SHIFT) jam_right = {{TOP{1'b0}},|v};
            else jam_right = (v >> amount) | {{TOP{1'b0}},|(v & ~({WINDOW{1'b1}} << amount))};
        end
    endfunction
    function [SHIFT_BITS-1:0] leading_zeros;
        input [TOP:0] v;
        integer i;
        begin
            leading_zeros = FULL_SHIFT;
            for (i=0; i<WINDOW; i=i+1) if (v[i]) leading_zeros = TOP[SHIFT_BITS-1:0] - i[SHIFT_BITS-1:0];
        end
    endfunction
    // Clamp a signed shift distance to 0..WINDOW. Negative distances occur
    // when a zero operand trails a smaller exponent; its shift is irrelevant.
    function [SHIFT_BITS-1:0] clamp_shift;
        input signed [12:0] distance;
        begin
            clamp_shift = distance < 0 ? {SHIFT_BITS{1'b0}} :
                          distance > $signed({6'b0,FULL_SHIFT}) ? FULL_SHIFT : distance[SHIFT_BITS-1:0];
        end
    endfunction
    task finish;
        input [31:0] value;
        input [4:0] exceptions;
        input bad_request;
        begin
            result <= value; flags <= exceptions; error <= bad_request;
            state <= RESPONSE;
        end
    endtask

    wire [35:0] unpack_x = unpack(x), unpack_y = unpack(y), unpack_z = unpack(z);
    wire signed [11:0] exp_x = $signed(unpack_x[35:24]);
    wire signed [11:0] exp_y = $signed(unpack_y[35:24]);
    wire signed [11:0] exp_z = $signed(unpack_z[35:24]);
    wire [23:0] sig_x = unpack_x[23:0], sig_y = unpack_y[23:0], sig_z = unpack_z[23:0];
    wire [47:0] product = sig_x * sig_y;
    wire product_sign = x[31] ^ y[31] ^ (operation == OP_FNMSUB || operation == OP_FNMADD);
    wire third_sign = z[31] ^ (operation == OP_FMSUB || operation == OP_FNMADD);
    wire second_sign = y[31] ^ (operation == OP_SUB);
    wire fused = operation >= OP_FMADD && operation <= OP_FNMADD;
    // Both addition operands as 48-bit magnitudes whose bit 47 weighs
    // 2^exponent: x or the normalized product, then y, z or nothing.
    wire is_add = operation == OP_ADD || operation == OP_SUB;
    wire [47:0] lhs_mag = is_add ? {sig_x,24'b0} : product[47] ? product : product << 1;
    wire signed [11:0] lhs_exp = is_add ? exp_x : exp_x + exp_y + (product[47] ? 12'sd1 : 12'sd0);
    wire [47:0] rhs_mag = is_add ? {sig_y,24'b0} : fused ? {sig_z,24'b0} : 48'b0;
    // A plain multiply has no second operand; z does not reach the datapath.
    wire signed [11:0] rhs_exp = is_add ? exp_y : fused ? exp_z : lhs_exp;
    // The larger exponent sets bit TOP-1 of the window; the other operand
    // shifts right by the gap. A zero operand never leads.
    wire lhs_leads = rhs_mag == 0 || (lhs_mag != 0 && lhs_exp >= rhs_exp);
    wire signed [12:0] exp_gap = $signed({lhs_exp[11],lhs_exp}) - $signed({rhs_exp[11],rhs_exp});
    // Load the trailing operand into magnitude, the leading one into addend,
    // with signs to match; COMBINE is symmetric in the two.
    task load_operands;
        input lhs_sign, rhs_sign;
        begin
            magnitude <= {1'b0,lhs_leads ? rhs_mag : lhs_mag,{OPERAND_LSB{1'b0}}};
            addend <= {1'b0,lhs_leads ? lhs_mag : rhs_mag,{OPERAND_LSB{1'b0}}};
            sign_result <= lhs_leads ? rhs_sign : lhs_sign;
            sign_addend <= lhs_leads ? lhs_sign : rhs_sign;
            align_shift <= clamp_shift(lhs_leads ? exp_gap : -exp_gap);
            exponent <= (lhs_leads ? lhs_exp : rhs_exp) + 12'sd1;
            state <= ALIGN;
        end
    endtask
    wire eq_xy = x == y || (is_zero(x) && is_zero(y));
    wire lt_xy = !eq_xy && ((x[31] != y[31]) ? x[31] :
                      (x[31] ? x[30:0] > y[30:0] : x[30:0] < y[30:0]));

    wire discarded = |magnitude[SIG_LSB-1:0];
    wire round_up = increment(mode,sign_result,magnitude[SIG_LSB],magnitude[SIG_LSB-1],|magnitude[SIG_LSB-2:0]);
    wire [24:0] rounded = {1'b0,magnitude[TOP:SIG_LSB]} + {24'b0,round_up};
    wire signed [11:0] final_exponent = exponent + (rounded[24] ? 12'sd1 : 12'sd0);
    wire [23:0] final_significand = rounded[24] ? rounded[24:1] : rounded[23:0];
    // Only the low eight bits are packed; final_exponent checks the upper range.
    wire [11:0] unused_biased_exponent = final_exponent + 12'd127;
    wire [7:0] packed_exponent = final_significand[23] ? unused_biased_exponent[7:0] : 8'b0;
    wire overflow_inf = mode == RM_RNE || mode == RM_RMM ||
                              (mode == RM_RDN && sign_result) || (mode == RM_RUP && !sign_result);

    wire integer_discarded = |magnitude[INT_LSB-1:0];
    wire integer_up = increment(mode,sign_result,magnitude[INT_LSB],magnitude[INT_LSB-1],|magnitude[INT_LSB-2:0]);
    wire [32:0] integer_rounded = {1'b0,magnitude[TOP:INT_LSB]} + {32'b0,integer_up};
    wire integer_invalid = operation == OP_F32_TO_U32 ?
        (integer_rounded[32] || (sign_result && |integer_rounded)) :
        (integer_rounded > (sign_result ? 33'h080000000 : 33'h07fffffff));
    wire [31:0] integer_limit = operation == OP_F32_TO_U32 ?
        (sign_result ? 32'b0 : 32'hffffffff) :
        (sign_result ? 32'h80000000 : 32'h7fffffff);

    wire div_bit = div_remainder >= {3'b0,divisor};
    wire [26:0] div_difference = div_bit ? div_remainder - {3'b0,divisor} : div_remainder;
    wire [26:0] next_quotient = {quotient[25:0],div_bit};
    wire [55:0] sqrt_step = {sqrt_remainder[53:0],radicand[53:52]};
    wire [55:0] sqrt_trial = {27'b0,root,2'b01};
    wire sqrt_bit = sqrt_step >= sqrt_trial;
    wire [55:0] sqrt_difference = sqrt_bit ? sqrt_step - sqrt_trial : sqrt_step;
    wire [26:0] next_root = {root[25:0],sqrt_bit};

    // One jam shifter serves ALIGN (trailing operand), DENORMALIZE (down to
    // exponent -126) and INT_SHIFT (up to exponent 31, the integer point).
    wire signed [12:0] jam_target = state == DENORMALIZE ? -13'sd126 : 13'sd31;
    wire signed [12:0] jam_distance = jam_target - $signed({exponent[11],exponent});
    wire [SHIFT_BITS-1:0] jam_amount = state == ALIGN ? align_shift : clamp_shift(jam_distance);
    wire [TOP:0] magnitude_jammed = jam_right(magnitude,jam_amount);
    wire [SHIFT_BITS-1:0] normalize_shift = leading_zeros(magnitude);

    always @(posedge clk) begin
        if (reset) begin
            tiny_after_rounding <= 0;
            state <= IDLE; result <= 0; flags <= 0; error <= 0;
            operation <= OP_ADD; mode <= 0; x <= 0; y <= 0; z <= 0;
            magnitude <= 0; addend <= 0; sign_result <= 0; sign_addend <= 0;
            align_shift <= 0; exponent <= 0; iteration <= 0;
            quotient <= 0; div_remainder <= 0; divisor <= 0;
            radicand <= 0; sqrt_remainder <= 0; root <= 0;
        end else begin
            case (state)
                IDLE: if (req_valid) begin
                    operation <= op; mode <= rm; x <= a; y <= b; z <= c;
                    state <= DECODE;
                end
                DECODE: begin
                    if (operation > OP_MAX || mode > RM_RMM) finish(0,0,1);
                    else if (operation == OP_ADD || operation == OP_SUB) begin
                        if (is_nan(x) || is_nan(y))
                            finish(32'h7fc00000,{is_snan(x)||is_snan(y),4'b0},0);
                        else if (is_inf(x) && is_inf(y) && x[31] != second_sign)
                            finish(32'h7fc00000,FLAG_NV,0);
                        else if (is_inf(x) || is_inf(y))
                            finish({is_inf(x) ? x[31] : second_sign,8'hff,23'b0},0,0);
                        else begin
                            load_operands(x[31],second_sign);
                        end
                    end else if (operation >= OP_MUL && operation <= OP_FNMADD) begin
                        if ((is_zero(x) && is_inf(y)) || (is_inf(x) && is_zero(y)))
                            finish(32'h7fc00000,FLAG_NV,0);
                        else if (is_nan(x) || is_nan(y) || (fused && is_nan(z)))
                            finish(32'h7fc00000,{is_snan(x)||is_snan(y)||(fused&&is_snan(z)),4'b0},0);
                        else if ((is_inf(x) || is_inf(y)) && fused && is_inf(z) && product_sign != third_sign)
                            finish(32'h7fc00000,FLAG_NV,0);
                        else if (is_inf(x) || is_inf(y) || (fused && is_inf(z)))
                            finish({(is_inf(x)||is_inf(y)) ? product_sign : third_sign,8'hff,23'b0},0,0);
                        else begin
                            load_operands(product_sign,fused ? third_sign : product_sign);
                        end
                    end else if (operation == OP_DIV) begin
                        sign_result <= x[31] ^ y[31];
                        if (is_nan(x) || is_nan(y))
                            finish(32'h7fc00000,{is_snan(x)||is_snan(y),4'b0},0);
                        else if ((is_zero(x) && is_zero(y)) || (is_inf(x) && is_inf(y)))
                            finish(32'h7fc00000,FLAG_NV,0);
                        else if (is_inf(x)) finish({x[31]^y[31],8'hff,23'b0},0,0);
                        else if (is_zero(y)) finish({x[31]^y[31],8'hff,23'b0},FLAG_DZ,0);
                        else if (is_zero(x) || is_inf(y)) finish({x[31]^y[31],31'b0},0,0);
                        else begin
                            div_remainder <= {3'b0,sig_x}; divisor <= sig_y;
                            quotient <= 0; iteration <= 6'd27;
                            exponent <= exp_x-exp_y; state <= DIVIDE;
                        end
                    end else if (operation == OP_SQRT) begin
                        sign_result <= 0;
                        if (is_nan(x)) finish(32'h7fc00000,{is_snan(x),4'b0},0);
                        else if (is_zero(x)) finish(x,0,0);
                        else if (x[31]) finish(32'h7fc00000,FLAG_NV,0);
                        else if (is_inf(x)) finish(x,0,0);
                        else begin
                            radicand <= exp_x[0] ? {sig_x,30'b0} : {1'b0,sig_x,29'b0};
                            exponent <= exp_x >>> 1;
                            root <= 0; sqrt_remainder <= 0; iteration <= 6'd27;
                            state <= SQRT;
                        end
                    end else if (operation == OP_I32_TO_F32 || operation == OP_U32_TO_F32) begin
                        sign_result <= operation == OP_I32_TO_F32 && x[31];
                        magnitude <= {((operation == OP_I32_TO_F32 && x[31]) ? (~x + 32'd1) : x),{INT_LSB{1'b0}}};
                        exponent <= 12'sd31; state <= NORMALIZE;
                    end else if (operation == OP_F32_TO_I32 || operation == OP_F32_TO_U32) begin
                        sign_result <= x[31];
                        if (is_nan(x)) finish(operation == OP_F32_TO_U32 ? 32'hffffffff : 32'h7fffffff,FLAG_NV,0);
                        else if (is_inf(x) || exp_x > 31)
                            finish(operation == OP_F32_TO_U32 ? (x[31] ? 32'b0 : 32'hffffffff) :
                                   (x[31] ? 32'h80000000 : 32'h7fffffff),FLAG_NV,0);
                        else begin
                            magnitude <= {sig_x,{SIG_LSB{1'b0}}}; exponent <= exp_x;
                            state <= INT_SHIFT;
                        end
                    end else if (operation >= OP_EQ && operation <= OP_LE) begin
                        if (is_nan(x) || is_nan(y))
                            finish(0,{operation != OP_EQ || is_snan(x) || is_snan(y),4'b0},0);
                        else finish({31'b0,(operation == OP_EQ ? eq_xy : operation == OP_LT ? lt_xy : (eq_xy||lt_xy))},0,0);
                    end else begin // FMIN/FMAX (op 16/17)
                        if (is_nan(x)) finish(is_nan(y) ? 32'h7fc00000 : y,{is_snan(x)||is_snan(y),4'b0},0);
                        else if (is_nan(y)) finish(x,{is_snan(y),4'b0},0);
                        else if (is_zero(x) && is_zero(y)) finish(operation == OP_MIN ? (x|y) : (x&y),0,0);
                        else finish((lt_xy ^ (operation == OP_MAX)) ? x : y,0,0);
                    end
                end
                ALIGN: begin
                    magnitude <= magnitude_jammed;
                    state <= COMBINE;
                end
                COMBINE: begin
                    if (sign_result == sign_addend) magnitude <= magnitude + addend;
                    else if (magnitude >= addend) begin
                        magnitude <= magnitude-addend;
                        if (magnitude == addend) sign_result <= mode == RM_RDN;
                    end else begin magnitude <= addend-magnitude; sign_result <= sign_addend; end
                    state <= NORMALIZE;
                end
                NORMALIZE: begin
                    if (magnitude == 0) finish({sign_result,31'b0},0,0);
                    else if (!magnitude[79]) begin
                        magnitude <= magnitude << normalize_shift;
                        exponent <= exponent - $signed({5'b0,normalize_shift});
                    end
                    else begin
                        // Tininess after rounding uses precision rounding with
                        // an unbounded exponent, before subnormal quantization.
                        tiny_after_rounding <= final_exponent < -12'sd126;
                        state <= DENORMALIZE;
                    end
                end
                // A second visit leaves for ROUND: ROUND reads the updated
                // exponent and tiny_after_rounding is already latched.
                DENORMALIZE: begin
                    if (exponent < -12'sd126) begin
                        magnitude <= magnitude_jammed;
                        exponent <= -12'sd126;
                    end else state <= ROUND;
                end
                ROUND: begin
                    if (final_exponent > 127)
                        finish({sign_result,overflow_inf ? 31'h7f800000 : 31'h7f7fffff},FLAG_OF|FLAG_NX,0);
                    else finish({sign_result,packed_exponent,final_significand[22:0]},
                                (discarded ? FLAG_NX : 5'b0) | ((discarded && tiny_after_rounding) ? FLAG_UF : 5'b0),0);
                end
                DIVIDE: begin
                    quotient <= next_quotient[25:0];
                    div_remainder <= div_difference << 1;
                    iteration <= iteration-6'd1;
                    if (iteration == 1) begin
                        magnitude <= {next_quotient,{TOP-27{1'b0}},|div_difference}; state <= NORMALIZE;
                    end
                end
                SQRT: begin
                    root <= next_root; sqrt_remainder <= sqrt_difference[53:0];
                    radicand <= radicand << 2; iteration <= iteration-6'd1;
                    if (iteration == 1) begin
                        magnitude <= {next_root,{TOP-27{1'b0}},|sqrt_difference}; state <= NORMALIZE;
                    end
                end
                // DECODE rejects exponents above 31, so the shift is never
                // negative and INT_ROUND does not read the exponent.
                INT_SHIFT: begin
                    magnitude <= magnitude_jammed;
                    state <= INT_ROUND;
                end
                INT_ROUND: begin
                    if (integer_invalid) finish(integer_limit,FLAG_NV,0);
                    else finish(sign_result ? (~integer_rounded[31:0]+32'd1) : integer_rounded[31:0],
                                {4'b0,integer_discarded},0);
                end
                RESPONSE: if (resp_ready) state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    // The exactness argument in docs/fp32.md rests on these placements.
    always @(posedge clk) if (!reset) begin
        if (state == ALIGN && (addend[TOP] || magnitude[TOP] || |addend[OPERAND_LSB-1:0] ||
                               (addend != 0 && !addend[TOP-1]))) begin
            $display("FP32 INVARIANT: misplaced addition operand %h %h",addend,magnitude);
            $finish;
        end
        if (state == INT_SHIFT && exponent > 12'sd31) begin
            $display("FP32 INVARIANT: integer shift from exponent %0d",exponent);
            $finish;
        end
    end
`endif
endmodule
