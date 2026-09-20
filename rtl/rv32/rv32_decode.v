`timescale 1ns/1ps

// Combinational instruction decoder: register fields, the sign-extended
// immediate for each format, one flag per M3 instruction class, and two
// stop conditions. `illegal` follows the machine contract (the emulator
// traps the same words); `unsupported` marks valid RV32I this slice does
// not execute yet and disappears in M4.
module rv32_decode (
    input  wire [31:0] insn,
    output wire [4:0]  rd,
    output wire [4:0]  rs1,
    output wire [4:0]  rs2,
    output reg  [31:0] imm,
    output wire        is_lui,
    output wire        is_auipc,
    output wire        is_alu_reg,   // add, sub
    output wire        alu_sub,
    output wire        is_load,      // lw
    output wire        is_store,     // sw, sb
    output wire        store_byte,
    output wire        is_branch,    // beq, bne
    output wire        branch_ne,
    output wire        is_jal,
    output wire        is_ecall,
    output wire        is_ebreak,
    output wire        writes_rd,
    output reg         illegal,
    output reg         unsupported
);
    localparam [6:0] OP_LUI = 7'h37, OP_AUIPC = 7'h17, OP_JAL = 7'h6F, OP_JALR = 7'h67,
                     OP_BRANCH = 7'h63, OP_LOAD = 7'h03, OP_STORE = 7'h23,
                     OP_IMM = 7'h13, OP_REG = 7'h33, OP_FENCE = 7'h0F, OP_SYSTEM = 7'h73;

    wire [6:0] opcode = insn[6:0];
    wire [2:0] funct3 = insn[14:12];
    wire [6:0] funct7 = insn[31:25];
    wire [11:0] csr = insn[31:20];

    assign rd = insn[11:7];
    assign rs1 = insn[19:15];
    assign rs2 = insn[24:20];

    // The four existing CSRs; any other number is illegal on the machine.
    wire csr_exists = (csr == 12'h305) || (csr == 12'h341) || (csr == 12'h342) || (csr == 12'h343);

    assign is_lui = (opcode == OP_LUI);
    assign is_auipc = (opcode == OP_AUIPC);
    wire is_alu_imm = (opcode == OP_IMM) && (funct3 == 3'd0); // addi: the ALU's immediate operand is the default
    assign is_alu_reg = (opcode == OP_REG) && (funct3 == 3'd0) && (funct7 == 7'd0 || funct7 == 7'h20);
    assign alu_sub = funct7[5];
    assign is_load = (opcode == OP_LOAD) && (funct3 == 3'd2);
    assign is_store = (opcode == OP_STORE) && (funct3 == 3'd0 || funct3 == 3'd2);
    assign store_byte = (funct3 == 3'd0);
    assign is_branch = (opcode == OP_BRANCH) && (funct3 == 3'd0 || funct3 == 3'd1);
    assign branch_ne = funct3[0];
    assign is_jal = (opcode == OP_JAL);
    assign is_ecall = (insn == 32'h00000073);
    assign is_ebreak = (insn == 32'h00100073);
    assign writes_rd = is_lui || is_auipc || is_alu_imm || is_alu_reg || is_load || is_jal;

    // Immediates: each format places the sign bit at insn[31], so every
    // extension replicates that one bit (RV32I chapter 2.3).
    always @* begin
        case (opcode)
            OP_LUI, OP_AUIPC: imm = {insn[31:12], 12'd0};
            OP_JAL: imm = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
            OP_BRANCH: imm = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
            OP_STORE: imm = {{21{insn[31]}}, insn[30:25], insn[11:7]};
            default: imm = {{21{insn[31]}}, insn[30:20]}; // I-type: loads, addi, jalr, system
        endcase
    end

    // Classify everything the slice does not execute. Assign both outputs on
    // every path so no latch is inferred.
    always @* begin
        illegal = 1'b0;
        unsupported = 1'b0;
        case (opcode)
            OP_LUI, OP_AUIPC, OP_JAL: begin end
            OP_JALR: if (funct3 == 3'd0) unsupported = 1'b1; else illegal = 1'b1;
            OP_BRANCH: case (funct3)
                3'd0, 3'd1: begin end
                3'd4, 3'd5, 3'd6, 3'd7: unsupported = 1'b1;
                default: illegal = 1'b1;
            endcase
            OP_LOAD: case (funct3)
                3'd2: begin end
                3'd0, 3'd1, 3'd4, 3'd5: unsupported = 1'b1;
                default: illegal = 1'b1;
            endcase
            OP_STORE: case (funct3)
                3'd0, 3'd2: begin end
                3'd1: unsupported = 1'b1;
                default: illegal = 1'b1;
            endcase
            OP_IMM: case (funct3)
                3'd0: begin end
                3'd1: if (funct7 == 7'd0) unsupported = 1'b1; else illegal = 1'b1;
                3'd5: if (funct7 == 7'd0 || funct7 == 7'h20) unsupported = 1'b1; else illegal = 1'b1;
                default: unsupported = 1'b1; // slti, sltiu, xori, ori, andi
            endcase
            OP_REG: begin
                if (funct7 == 7'd0)
                    unsupported = (funct3 != 3'd0); // add is ours; the rest wait for M4
                else if (funct7 == 7'h20 && (funct3 == 3'd0 || funct3 == 3'd5))
                    unsupported = (funct3 == 3'd5); // sub is ours, sra is not
                else
                    illegal = 1'b1; // includes every M-extension word (funct7 == 1)
            end
            OP_FENCE: if (funct3 == 3'd0) unsupported = 1'b1; else illegal = 1'b1; // fence.i is illegal
            OP_SYSTEM: begin
                if (funct3 == 3'd0) begin
                    if (insn == 32'h30200073) unsupported = 1'b1;          // mret
                    else if (!is_ecall && !is_ebreak) illegal = 1'b1;       // wfi, sret, ...
                end else if (funct3 == 3'd4 || !csr_exists) begin
                    illegal = 1'b1;
                end else begin
                    unsupported = 1'b1;                                     // csrrw/s/c and immediates
                end
            end
            default: illegal = 1'b1;
        endcase
    end
endmodule
