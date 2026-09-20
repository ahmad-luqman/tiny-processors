`timescale 1ns/1ps

// Combinational instruction decoder: register fields, the sign-extended
// immediate for each format, one flag per instruction class, and the
// `illegal` check that follows the machine contract (the emulator traps the
// same words). Every valid word sets exactly one class flag or `illegal`;
// the core executes all of them.
module rv32_decode (
    input  wire [31:0] insn,
    output wire [4:0]  rd,
    output wire [4:0]  rs1,
    output wire [4:0]  rs2,
    output wire [2:0]  funct3,
    output reg  [31:0] imm,
    output wire        is_lui,
    output wire        is_auipc,
    output wire        is_alu_imm,   // addi slti sltiu xori ori andi slli srli srai
    output wire        is_alu_reg,   // add sub sll slt sltu xor srl sra or and
    output wire        alu_alt,      // funct7[5] where it selects sub or sra
    output wire        is_load,      // lb lh lw lbu lhu
    output wire        is_store,     // sb sh sw
    output wire        is_branch,    // beq bne blt bge bltu bgeu
    output wire        is_jal,
    output wire        is_jalr,
    output wire        is_csr,       // csrrw csrrs csrrc and the immediate forms
    output wire        is_mret,
    output wire        is_ecall,
    output wire        is_ebreak,
    output wire        writes_rd,
    output reg         illegal
);
    localparam [6:0] OP_LUI = 7'h37, OP_AUIPC = 7'h17, OP_JAL = 7'h6F, OP_JALR = 7'h67,
                     OP_BRANCH = 7'h63, OP_LOAD = 7'h03, OP_STORE = 7'h23,
                     OP_IMM = 7'h13, OP_REG = 7'h33, OP_FENCE = 7'h0F, OP_SYSTEM = 7'h73;

    wire [6:0] opcode = insn[6:0];
    wire [6:0] funct7 = insn[31:25];
    wire [11:0] csr = insn[31:20];

    assign rd = insn[11:7];
    assign rs1 = insn[19:15];
    assign rs2 = insn[24:20];
    assign funct3 = insn[14:12];

    // The four existing CSRs; any other number is illegal on the machine.
    wire csr_exists = (csr == 12'h305) || (csr == 12'h341) || (csr == 12'h342) || (csr == 12'h343);
    assign is_mret = (insn == 32'h30200073);

    assign is_lui = (opcode == OP_LUI);
    assign is_auipc = (opcode == OP_AUIPC);
    assign is_alu_imm = (opcode == OP_IMM) && !illegal;
    assign is_alu_reg = (opcode == OP_REG) && !illegal;
    assign alu_alt = funct7[5] && (is_alu_reg || (is_alu_imm && funct3 == 3'd5));
    assign is_load = (opcode == OP_LOAD) && !illegal;
    assign is_store = (opcode == OP_STORE) && !illegal;
    assign is_branch = (opcode == OP_BRANCH) && !illegal;
    assign is_jal = (opcode == OP_JAL);
    assign is_jalr = (opcode == OP_JALR) && !illegal;
    assign is_csr = (opcode == OP_SYSTEM) && (funct3 != 3'd0) && !illegal;
    assign is_ecall = (insn == 32'h00000073);
    assign is_ebreak = (insn == 32'h00100073);
    assign writes_rd = is_lui || is_auipc || is_alu_imm || is_alu_reg || is_load || is_jal || is_jalr || is_csr;

    // Immediates: each format places the sign bit at insn[31], so every
    // extension replicates that one bit (RV32I chapter 2.3).
    always @* begin
        case (opcode)
            OP_LUI, OP_AUIPC: imm = {insn[31:12], 12'd0};
            OP_JAL: imm = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
            OP_BRANCH: imm = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
            OP_STORE: imm = {{21{insn[31]}}, insn[30:25], insn[11:7]};
            default: imm = {{21{insn[31]}}, insn[30:20]}; // I-type: loads, OP-IMM, jalr, system
        endcase
    end

    // What the machine rejects, opcode by opcode, mirroring the emulator's
    // `goto illegal` paths in tools/rv32emu.c. Every path assigns `illegal`.
    always @* begin
        case (opcode)
            OP_LUI, OP_AUIPC, OP_JAL: illegal = 1'b0;
            OP_JALR: illegal = (funct3 != 3'd0);
            OP_BRANCH: illegal = (funct3 == 3'd2) || (funct3 == 3'd3);
            OP_LOAD: illegal = (funct3 == 3'd3) || (funct3 > 3'd5);
            OP_STORE: illegal = (funct3 > 3'd2);
            OP_IMM: illegal = (funct3 == 3'd1 && funct7 != 7'd0) ||                    // slli with shamt bits set
                              (funct3 == 3'd5 && funct7 != 7'd0 && funct7 != 7'h20);   // neither srli nor srai
            OP_REG: illegal = !(funct7 == 7'd0 || (funct7 == 7'h20 && (funct3 == 3'd0 || funct3 == 3'd5))); // every M word
            OP_FENCE: illegal = (funct3 != 3'd0);                                      // fence.i and the rest
            OP_SYSTEM: illegal = (funct3 == 3'd0) ? !(is_ecall || is_ebreak || is_mret) // wfi, sret, odd fields
                                                   : (funct3 == 3'd4 || !csr_exists);  // CSR ops on missing CSRs
            default: illegal = 1'b1;
        endcase
    end

endmodule
