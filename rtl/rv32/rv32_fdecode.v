`timescale 1ns/1ps
// F compute decode. Memory stays in the main decoder. direct: 0 arithmetic,
// 1 sign injection, 2 move to integer, 3 classification, 4 move from integer.
module rv32_fdecode (
    input wire [31:0] insn, input wire [2:0] frm,
    output reg valid, output reg [4:0] op, output wire [2:0] rm,
    output reg to_integer, output reg from_integer, output reg [2:0] direct
);
    wire [6:0] opcode = insn[6:0], funct7 = insn[31:25];
    wire [4:0] rs2 = insn[24:20];
    wire [2:0] funct3 = insn[14:12];
    reg rounded;
    wire [2:0] resolved = funct3 == 3'd7 ? frm : funct3;
    assign rm = rounded ? resolved : 3'd0;
    always @* begin
        valid = 1'b0; op = 5'd0; to_integer = 1'b0;
        from_integer = 1'b0; direct = 3'd0; rounded = 1'b0;
        case (opcode)
            7'h43, 7'h47, 7'h4b, 7'h4f: begin
                valid = insn[26:25] == 2'b00; rounded = 1'b1;
                case (opcode)
                    7'h43: op = 5'd3;
                    7'h47: op = 5'd4;
                    7'h4b: op = 5'd5;
                    default: op = 5'd6;
                endcase
            end
            7'h53: begin
                valid = 1'b1;
                case (funct7)
                    7'h00: begin op = 5'd0; rounded = 1'b1; end
                    7'h04: begin op = 5'd1; rounded = 1'b1; end
                    7'h08: begin op = 5'd2; rounded = 1'b1; end
                    7'h0c: begin op = 5'd7; rounded = 1'b1; end
                    7'h2c: begin op = 5'd8; rounded = 1'b1; valid = rs2 == 5'd0; end
                    7'h60: begin
                        op = rs2[0] ? 5'd12 : 5'd11; rounded = 1'b1;
                        to_integer = 1'b1; valid = rs2 <= 5'd1;
                    end
                    7'h68: begin
                        op = rs2[0] ? 5'd10 : 5'd9; rounded = 1'b1;
                        from_integer = 1'b1; valid = rs2 <= 5'd1;
                    end
                    7'h50: begin
                        op = funct3 == 3'd2 ? 5'd13 : funct3 == 3'd1 ? 5'd14 : 5'd15;
                        to_integer = 1'b1; valid = funct3 <= 3'd2;
                    end
                    7'h14: begin op = funct3[0] ? 5'd17 : 5'd16; valid = funct3 <= 3'd1; end
                    7'h10: begin direct = 3'd1; valid = funct3 <= 3'd2; end
                    7'h70: begin
                        direct = funct3 == 3'd0 ? 3'd2 : 3'd3; to_integer = 1'b1;
                        valid = rs2 == 5'd0 && funct3 <= 3'd1;
                    end
                    7'h78: begin
                        direct = 3'd4; from_integer = 1'b1;
                        valid = rs2 == 5'd0 && funct3 == 3'd0;
                    end
                    default: valid = 1'b0;
                endcase
            end
            default: begin end
        endcase
        if (rounded && resolved > 3'd4) valid = 1'b0;
    end
    // Register fields and rd do not affect the validity of a compute instruction.
    wire unused_fields = &{1'b0, insn[24:7]};
endmodule
