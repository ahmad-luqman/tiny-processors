`timescale 1ns/1ps
// F compute decode. Memory stays in the main decoder. Named direct codes
// select the non-arithmetic path; op uses the standalone FP32 interface.
module rv32_fdecode (
    input wire [31:0] insn, input wire [2:0] frm,
    output reg valid, output reg [4:0] op, output wire [2:0] rm,
    output reg to_integer, output reg from_integer, output reg [2:0] direct
);
    `include "fp32_ops.vh"
    localparam [2:0] DIRECT_NONE=3'd0, DIRECT_SIGN=3'd1, DIRECT_TO_INT=3'd2,
                     DIRECT_CLASS=3'd3, DIRECT_FROM_INT=3'd4;
    wire [6:0] opcode = insn[6:0], funct7 = insn[31:25];
    wire [4:0] rs2 = insn[24:20];
    wire [2:0] funct3 = insn[14:12];
    reg rounded;
    wire [2:0] resolved = funct3 == 3'd7 ? frm : funct3;
    assign rm = rounded ? resolved : 3'd0;
    always @* begin
        valid = 1'b0; op = OP_ADD; to_integer = 1'b0;
        from_integer = 1'b0; direct = DIRECT_NONE; rounded = 1'b0;
        case (opcode)
            7'h43, 7'h47, 7'h4b, 7'h4f: begin
                valid = insn[26:25] == 2'b00; rounded = 1'b1;
                case (opcode)
                    7'h43: op = OP_FMADD;
                    7'h47: op = OP_FMSUB;
                    7'h4b: op = OP_FNMSUB;
                    default: op = OP_FNMADD;
                endcase
            end
            7'h53: begin
                valid = 1'b1;
                case (funct7)
                    7'h00: begin op = OP_ADD; rounded = 1'b1; end
                    7'h04: begin op = OP_SUB; rounded = 1'b1; end
                    7'h08: begin op = OP_MUL; rounded = 1'b1; end
                    7'h0c: begin op = OP_DIV; rounded = 1'b1; end
                    7'h2c: begin op = OP_SQRT; rounded = 1'b1; valid = rs2 == 5'd0; end
                    7'h60: begin
                        op = rs2[0] ? OP_F32_TO_U32 : OP_F32_TO_I32; rounded = 1'b1;
                        to_integer = 1'b1; valid = rs2 <= 5'd1;
                    end
                    7'h68: begin
                        op = rs2[0] ? OP_U32_TO_F32 : OP_I32_TO_F32; rounded = 1'b1;
                        from_integer = 1'b1; valid = rs2 <= 5'd1;
                    end
                    7'h50: begin
                        op = funct3 == 3'd2 ? OP_EQ : funct3 == 3'd1 ? OP_LT : OP_LE;
                        to_integer = 1'b1; valid = funct3 <= 3'd2;
                    end
                    7'h14: begin op = funct3[0] ? OP_MAX : OP_MIN; valid = funct3 <= 3'd1; end
                    7'h10: begin direct = DIRECT_SIGN; valid = funct3 <= 3'd2; end
                    7'h70: begin
                        direct = funct3 == 3'd0 ? DIRECT_TO_INT : DIRECT_CLASS; to_integer = 1'b1;
                        valid = rs2 == 5'd0 && funct3 <= 3'd1;
                    end
                    7'h78: begin
                        direct = DIRECT_FROM_INT; from_integer = 1'b1;
                        valid = rs2 == 5'd0 && funct3 == 3'd0;
                    end
                    default: valid = 1'b0;
                endcase
            end
            default: begin end
        endcase
        if (rounded && resolved > 3'd4) valid = 1'b0;
    end
    // rs1 and rd are arbitrary register indices; rs2/funct3 can constrain legality.
    wire unused_fields = &{1'b0, insn[19:15], insn[11:7]};
endmodule
