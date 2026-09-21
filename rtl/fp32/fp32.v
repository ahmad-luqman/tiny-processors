`timescale 1ns/1ps

// Standalone multicycle binary32 hardware. Contract and bit weights: docs/fp32.md.
// The wide exact accumulator is deliberately serially aligned/normalized; it
// retains cancellation bits in FMA and never rounds the product separately.
module fp32 (
    input wire clk, input wire reset,
    input wire req_valid, output wire req_ready,
    input wire [4:0] op, input wire [2:0] rm,
    input wire [31:0] a, b, c,
    output wire resp_valid, input wire resp_ready,
    output reg [31:0] result, output reg [4:0] flags, output reg error
);
    `include "fp32_states.vh"
    localparam [4:0] OP_ADD=5'd0,
        OP_SUB=5'd1,
        OP_MUL=5'd2,
        OP_FMADD=5'd3,
        OP_FMSUB=5'd4,
        OP_FNMSUB=5'd5,
        OP_FNMADD=5'd6,
        OP_DIV=5'd7,
        OP_SQRT=5'd8,
        OP_I32_TO_F32=5'd9,
        OP_U32_TO_F32=5'd10,
        OP_F32_TO_I32=5'd11,
        OP_F32_TO_U32=5'd12,
        OP_EQ=5'd13,
        OP_LT=5'd14,
        OP_LE=5'd15,
        OP_MIN=5'd16,
        OP_MAX=5'd17;
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
    reg [$clog2(STATE_COUNT)-1:0] state;
    reg [4:0] operation;
    reg [2:0] mode;
    reg [31:0] x, y, z;
    reg [575:0] magnitude, addend;
    reg sign_result, sign_addend;
    reg tiny_after_rounding;
    reg [9:0] shift_main, shift_addend;
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
    task finish;
        input [31:0] value;
        input [4:0] exceptions;
        input bad_request;
        begin
            result <= value; flags <= exceptions; error <= bad_request;
            state <= RESPONSE;
        end
    endtask

    wire [35:0] unpack_x = unpack(x), unpack_y = unpack(y);
    wire signed [11:0] exp_x = $signed(unpack_x[35:24]);
    wire signed [11:0] exp_y = $signed(unpack_y[35:24]);
    wire [23:0] sig_x = unpack_x[23:0], sig_y = unpack_y[23:0];
    wire [47:0] product = significand(x) * significand(y);
    wire product_sign = x[31] ^ y[31] ^ (operation == OP_FNMSUB || operation == OP_FNMADD);
    wire third_sign = z[31] ^ (operation == OP_FMSUB || operation == OP_FNMADD);
    wire second_sign = y[31] ^ (operation == OP_SUB);
    wire fused = operation >= OP_FMADD && operation <= OP_FNMADD;
    wire eq_xy = x == y || (is_zero(x) && is_zero(y));
    wire lt_xy = !eq_xy && ((x[31] != y[31]) ? x[31] :
                      (x[31] ? x[30:0] > y[30:0] : x[30:0] < y[30:0]));

    wire discarded = |magnitude[551:0];
    wire round_up = increment(mode,sign_result,magnitude[552],magnitude[551],|magnitude[550:0]);
    wire [24:0] rounded = {1'b0,magnitude[575:552]} + {24'b0,round_up};
    wire signed [11:0] final_exponent = exponent + (rounded[24] ? 12'sd1 : 12'sd0);
    wire [23:0] final_significand = rounded[24] ? rounded[24:1] : rounded[23:0];
    // Only the low eight bits are packed; final_exponent checks the upper range.
    wire [11:0] unused_biased_exponent = final_exponent + 12'd127;
    wire [7:0] packed_exponent = final_significand[23] ? unused_biased_exponent[7:0] : 8'b0;
    wire overflow_inf = mode == RM_RNE || mode == RM_RMM ||
                              (mode == RM_RDN && sign_result) || (mode == RM_RUP && !sign_result);

    wire integer_discarded = |magnitude[543:0];
    wire integer_up = increment(mode,sign_result,magnitude[544],magnitude[543],|magnitude[542:0]);
    wire [32:0] integer_rounded = {1'b0,magnitude[575:544]} + {32'b0,integer_up};
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

    always @(posedge clk) begin
        if (reset) begin
            tiny_after_rounding <= 0;
            state <= IDLE; result <= 0; flags <= 0; error <= 0;
            operation <= OP_ADD; mode <= 0; x <= 0; y <= 0; z <= 0;
            magnitude <= 0; addend <= 0; sign_result <= 0; sign_addend <= 0;
            shift_main <= 0; shift_addend <= 0; exponent <= 0; iteration <= 0;
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
                            magnitude <= {552'b0,significand(x)};
                            addend <= {552'b0,significand(y)};
                            shift_main <= effective_exp(x) + 10'd148;
                            shift_addend <= effective_exp(y) + 10'd148;
                            sign_result <= x[31]; sign_addend <= second_sign;
                            state <= ALIGN;
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
                            magnitude <= {528'b0,product};
                            shift_main <= effective_exp(x) + effective_exp(y) - 10'd2;
                            sign_result <= product_sign;
                            addend <= fused ? {552'b0,significand(z)} : 576'b0;
                            shift_addend <= fused ? effective_exp(z) + 10'd148 : 10'b0;
                            sign_addend <= fused ? third_sign : product_sign;
                            state <= ALIGN;
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
                        magnitude <= {((operation == OP_I32_TO_F32 && x[31]) ? (~x + 32'd1) : x),544'b0};
                        exponent <= 12'sd31; state <= NORMALIZE;
                    end else if (operation == OP_F32_TO_I32 || operation == OP_F32_TO_U32) begin
                        sign_result <= x[31];
                        if (is_nan(x)) finish(operation == OP_F32_TO_U32 ? 32'hffffffff : 32'h7fffffff,FLAG_NV,0);
                        else if (is_inf(x) || exp_x > 31)
                            finish(operation == OP_F32_TO_U32 ? (x[31] ? 32'b0 : 32'hffffffff) :
                                   (x[31] ? 32'h80000000 : 32'h7fffffff),FLAG_NV,0);
                        else begin
                            magnitude <= {sig_x,552'b0}; exponent <= exp_x;
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
                    if (shift_main != 0) begin magnitude <= magnitude << 1; shift_main <= shift_main-10'd1; end
                    if (shift_addend != 0) begin addend <= addend << 1; shift_addend <= shift_addend-10'd1; end
                    if (shift_main == 0 && shift_addend == 0) state <= COMBINE;
                end
                COMBINE: begin
                    if (sign_result == sign_addend) magnitude <= magnitude + addend;
                    else if (magnitude >= addend) begin
                        magnitude <= magnitude-addend;
                        if (magnitude == addend) sign_result <= mode == RM_RDN;
                    end else begin magnitude <= addend-magnitude; sign_result <= sign_addend; end
                    exponent <= 12'sd277; state <= NORMALIZE;
                end
                NORMALIZE: begin
                    if (magnitude == 0) finish({sign_result,31'b0},0,0);
                    else if (!magnitude[575]) begin magnitude <= magnitude << 1; exponent <= exponent-12'sd1; end
                    else begin
                        // Tininess after rounding uses precision rounding with
                        // an unbounded exponent, before subnormal quantization.
                        tiny_after_rounding <= final_exponent < -12'sd126;
                        state <= DENORMALIZE;
                    end
                end
                DENORMALIZE: begin
                    if (exponent < -12'sd126) begin
                        magnitude <= {1'b0,magnitude[575:2],|magnitude[1:0]};
                        exponent <= exponent+12'sd1;
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
                        magnitude <= {next_quotient,548'b0,|div_difference}; state <= NORMALIZE;
                    end
                end
                SQRT: begin
                    root <= next_root; sqrt_remainder <= sqrt_difference[53:0];
                    radicand <= radicand << 2; iteration <= iteration-6'd1;
                    if (iteration == 1) begin
                        magnitude <= {next_root,548'b0,|sqrt_difference}; state <= NORMALIZE;
                    end
                end
                INT_SHIFT: begin
                    if (exponent < 31) begin
                        magnitude <= {1'b0,magnitude[575:2],|magnitude[1:0]};
                        exponent <= exponent+12'sd1;
                    end else state <= INT_ROUND;
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
endmodule
