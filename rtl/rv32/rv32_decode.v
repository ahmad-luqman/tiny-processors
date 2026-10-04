`timescale 1ns/1ps

// Integer/memory instruction decode and sign-extended immediates. Legal words
// handled here select a class, except FENCE, which retires without effects.
// F compute words are decoded separately by rv32_fdecode; the core traps only
// when this decoder reports illegal and that decoder reports !fp_valid.
// The A extension (issue #34) is decoded here: LR.W is a load and SC.W and the
// AMOs are stores, each with its own flag as well, and their immediate is zero.
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
    output wire        is_muldiv,    // mul mulh mulhsu mulhu div divu rem remu (funct7 1)
    output wire        alu_alt,      // funct7[5] where it selects sub or sra
    output wire        is_load,      // lb lh lw lbu lhu, flw, lr.w
    output wire        is_store,     // sb sh sw, fsw, sc.w and the AMOs
    output wire        is_lr,        // issue #34: lr.w
    output wire        is_sc,        // sc.w
    output wire        is_amo,       // amoswap amoadd amoxor amoand amoor amomin amomax amominu amomaxu (.w)
    output wire        is_branch,    // beq bne blt bge bltu bgeu
    output wire        is_jal,
    output wire        is_jalr,
    output wire        is_csr,       // csrrw csrrs csrrc and the immediate forms
    output wire        is_mret,
    output wire        is_ecall,
    output wire        is_ebreak,
    output wire        is_wfi,
    output wire        is_sret,      // issue #20: S-mode
    output wire        is_sfence,    // sfence.vma, any rs1 and rs2
    output wire        writes_rd,
    output reg         illegal
);
    localparam [6:0] OP_LUI = 7'h37, OP_AUIPC = 7'h17, OP_JAL = 7'h6F, OP_JALR = 7'h67,
                     OP_BRANCH = 7'h63, OP_LOAD = 7'h03, OP_STORE = 7'h23,
                     OP_IMM = 7'h13, OP_REG = 7'h33, OP_FENCE = 7'h0F, OP_SYSTEM = 7'h73,
                     OP_AMO = 7'h2F;
    // funct5 of OP_AMO (funct3 2, a word); aq and rl below it change nothing on one hart.
    localparam [4:0] A_LR = 5'b00010, A_SC = 5'b00011;

    wire [6:0] opcode = insn[6:0];
    wire [6:0] funct7 = insn[31:25];
    wire [11:0] csr = insn[31:20];
    wire [4:0] funct5 = insn[31:27];
    wire amo_funct5 = funct5 == 5'b00000 || funct5 == 5'b00001 || funct5 == 5'b00100 || funct5 == 5'b01100 ||
                      funct5 == 5'b01000 || funct5 == 5'b10000 || funct5 == 5'b10100 || funct5 == 5'b11000 ||
                      funct5 == 5'b11100;

    assign rd = insn[11:7];
    assign rs1 = insn[19:15];
    assign rs2 = insn[24:20];
    assign funct3 = insn[14:12];

    // The trap CSRs, the interrupt CSRs of O1 (mstatus, mie, mscratch, mip), O5's mcounteren,
    // pmpcfg0-1 and pmpaddr0-7, the three
    // floating aliases, and the six Zicntr counters (cycle, time, instret and their high
    // halves); since issue #20 medeleg, mideleg and the supervisor's CSRs (sstatus, sie, stvec,
    // scounteren, sscratch, sepc, scause, stval, sip, satp); other numbers are illegal.
    wire csr_exists = (csr == 12'h001) || (csr == 12'h002) || (csr == 12'h003) || (csr == 12'h305) || (csr == 12'h341) || (csr == 12'h342) || (csr == 12'h343) ||
                      (csr == 12'h300) || (csr == 12'h304) || (csr == 12'h340) || (csr == 12'h344) ||
                      (csr == 12'h306) || (csr == 12'h3a0) || (csr == 12'h3a1) || (csr[11:3] == 9'h076) || // O5: mcounteren, PMP
                      (csr == 12'hc00) || (csr == 12'hc01) || (csr == 12'hc02) || (csr == 12'hc80) || (csr == 12'hc81) || (csr == 12'hc82) ||
                      (csr == 12'h302) || (csr == 12'h303) || (csr == 12'h100) || (csr == 12'h180) ||
                      (csr[11:3] == 9'h020 && csr[2:0] >= 3'd4 && csr[2:0] <= 3'd6) || // sie, stvec, scounteren
                      (csr[11:3] == 9'h028 && csr[2:0] <= 3'd4);                         // sscratch, sepc, scause, stval, sip
    // CSR numbers with bits [11:10] set are read-only; csrrw always writes, and
    // csrrs/csrrc (and the immediate forms) write when the rs1 field is nonzero.
    wire csr_write_to_read_only = (csr[11:10] == 2'b11) && ((funct3[1:0] == 2'd1) || (rs1 != 5'd0));
    assign is_mret = (insn == 32'h30200073);

    assign is_lui = (opcode == OP_LUI);
    assign is_auipc = (opcode == OP_AUIPC);
    assign is_alu_imm = (opcode == OP_IMM) && !illegal;
    assign is_muldiv = (opcode == OP_REG) && (funct7 == 7'd1);
    assign is_alu_reg = (opcode == OP_REG) && !illegal && !is_muldiv;
    assign alu_alt = funct7[5] && (is_alu_reg || (is_alu_imm && funct3 == 3'd5));
    assign is_lr = (opcode == OP_AMO) && (funct5 == A_LR) && !illegal;
    assign is_sc = (opcode == OP_AMO) && (funct5 == A_SC) && !illegal;
    assign is_amo = (opcode == OP_AMO) && amo_funct5 && !illegal;
    assign is_load = ((opcode == OP_LOAD || opcode == 7'h07) && !illegal) || is_lr;
    assign is_store = ((opcode == OP_STORE || opcode == 7'h27) && !illegal) || is_sc || is_amo;
    assign is_branch = (opcode == OP_BRANCH) && !illegal;
    assign is_jal = (opcode == OP_JAL);
    assign is_jalr = (opcode == OP_JALR) && !illegal;
    assign is_csr = (opcode == OP_SYSTEM) && (funct3 != 3'd0) && !illegal;
    assign is_ecall = (insn == 32'h00000073);
    assign is_ebreak = (insn == 32'h00100073);
    assign is_wfi = (insn == 32'h10500073);
    assign is_sret = (insn == 32'h10200073);
    assign is_sfence = (insn & 32'hfe007fff) == 32'h12000073;
    assign writes_rd = is_lui || is_auipc || is_alu_imm || is_alu_reg || is_muldiv || (is_load && opcode == OP_LOAD) || is_jal || is_jalr || is_csr ||
                       is_lr || is_sc || is_amo;

    // Immediates: each format places the sign bit at insn[31], so every
    // extension replicates that one bit (RV32I chapter 2.3).
    always @* begin
        case (opcode)
            OP_LUI, OP_AUIPC: imm = {insn[31:12], 12'd0};
            OP_JAL: imm = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
            OP_BRANCH: imm = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
            OP_STORE, 7'h27: imm = {{21{insn[31]}}, insn[30:25], insn[11:7]};
            OP_AMO: imm = 32'd0; // the address is rs1 alone
            default: imm = {{21{insn[31]}}, insn[30:20]}; // I-type: loads, OP-IMM, jalr, system
        endcase
    end

    // What the machine rejects, opcode by opcode, mirroring the emulator's
    // `goto illegal` paths in tools/rv32emu_core.c. Every path assigns `illegal`.
    always @* begin
        case (opcode)
            OP_LUI, OP_AUIPC, OP_JAL: illegal = 1'b0;
            OP_JALR: illegal = (funct3 != 3'd0);
            OP_BRANCH: illegal = (funct3 == 3'd2) || (funct3 == 3'd3);
            7'h07, 7'h27: illegal = funct3 != 3'd2;
            OP_LOAD: illegal = (funct3 == 3'd3) || (funct3 > 3'd5);
            OP_STORE: illegal = (funct3 > 3'd2);
            OP_IMM: illegal = (funct3 == 3'd1 && funct7 != 7'd0) ||                    // slli with bits above the shift amount set
                              (funct3 == 3'd5 && funct7 != 7'd0 && funct7 != 7'h20);   // neither srli nor srai
            OP_REG: illegal = !(funct7 == 7'd0 || funct7 == 7'd1 ||                      // funct7 1 is the M extension
                                (funct7 == 7'h20 && (funct3 == 3'd0 || funct3 == 3'd5)));
            OP_FENCE: illegal = (funct3 != 3'd0);                                      // fence.i and the rest
            OP_AMO: illegal = funct3 != 3'd2 || !(amo_funct5 || funct5 == A_SC ||       // words only, and
                                                  (funct5 == A_LR && rs2 == 5'd0));    // lr.w with rs2 0
            OP_SYSTEM: illegal = (funct3 == 3'd0) ? !(is_ecall || is_ebreak || is_mret || is_wfi || is_sret || is_sfence)
                                                   : (funct3 == 3'd4 || !csr_exists ||  // CSR ops on missing CSRs
                                                      csr_write_to_read_only);          // and writes to the counters
            default: illegal = 1'b1;
        endcase
    end

endmodule
