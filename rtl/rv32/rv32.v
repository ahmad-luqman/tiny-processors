`timescale 1ns/1ps

// Multicycle RV32I core: FETCH, DECODE, EXECUTE, MEM, WRITEBACK, one
// ready/valid memory port with byte strobes in both directions, the four
// trap CSRs, and retirement in the last state. A trap vectors to mtvec; a
// second trap before the handler retires an instruction halts the core
// (the emulator's double-fault rule). The contract is docs/rv32-rtl.md;
// the datapath and controller are explained in docs/rv32-to-gates.md.
module rv32 (
    input  wire        clk,
    input  wire        reset,
    // Memory port (docs/rv32.md, "Memory transaction contract").
    output wire        mem_valid,
    output wire [31:0] mem_addr,
    output wire        mem_we,
    output wire [3:0]  mem_strb,
    output wire [31:0] mem_wdata,
    input  wire        mem_ready,
    input  wire [31:0] mem_rdata,
    input  wire        mem_error,
    output wire        mem_fetch,
    // Retirement port for the testbench.
    output reg         retire,
    output reg  [31:0] retire_pc,
    output reg  [31:0] retire_insn,
    output reg         retire_rd_we,
    output reg  [4:0]  retire_rd,
    output reg  [31:0] retire_rd_value,
    output reg         trap,
    output reg  [3:0]  trap_cause,
    output reg  [31:0] trap_value,
    output reg         halted,
    output reg  [2:0]  state,
    output reg  [31:0] pc,
    output reg  [31:0] mtvec,
    output reg  [31:0] mepc,
    output reg  [31:0] mcause,
    output reg  [31:0] mtval
);
    localparam [2:0] FETCH = 3'd0, DECODE = 3'd1, EXECUTE = 3'd2,
                     MEM = 3'd3, WRITEBACK = 3'd4, HALT = 3'd5;
    localparam [3:0] CAUSE_TARGET_MISALIGNED = 4'd0, CAUSE_FETCH_FAULT = 4'd1,
                     CAUSE_ILLEGAL = 4'd2, CAUSE_BREAKPOINT = 4'd3,
                     CAUSE_LOAD_MISALIGNED = 4'd4, CAUSE_LOAD_FAULT = 4'd5,
                     CAUSE_STORE_MISALIGNED = 4'd6, CAUSE_STORE_FAULT = 4'd7,
                     CAUSE_ECALL = 4'd11;
    localparam [11:0] CSR_MTVEC = 12'h305, CSR_MEPC = 12'h341, CSR_MCAUSE = 12'h342; // 12'h343 is mtval, the default
    localparam [31:0] RESET_PC = 32'h8000_0000;

    // Datapath registers: one instruction's worth of state between states.
    reg [31:0] ir, ir_pc, a, b, alu_out, mdr;
    reg taken;
    reg in_trap; // a trap was taken and its handler has not retired an instruction yet

    // Decoded fields, combinational from ir.
    wire [4:0] rd, rs1, rs2;
    wire [2:0] funct3;
    wire [31:0] imm;
    wire is_lui, is_auipc, is_alu_imm, is_alu_reg, alu_alt, is_load, is_store;
    wire is_branch, is_jal, is_jalr, is_csr, is_mret, is_ecall, is_ebreak, writes_rd, illegal;

    rv32_decode decode (
        .insn(ir), .rd(rd), .rs1(rs1), .rs2(rs2), .funct3(funct3), .imm(imm),
        .is_lui(is_lui), .is_auipc(is_auipc), .is_alu_imm(is_alu_imm), .is_alu_reg(is_alu_reg),
        .alu_alt(alu_alt), .is_load(is_load), .is_store(is_store), .is_branch(is_branch),
        .is_jal(is_jal), .is_jalr(is_jalr), .is_csr(is_csr), .is_mret(is_mret),
        .is_ecall(is_ecall), .is_ebreak(is_ebreak), .writes_rd(writes_rd), .illegal(illegal));

    // Access width from funct3[1:0] (0 byte, 1 halfword, 2 word) and the lane
    // the effective address selects; shared by loads and stores.
    wire [1:0] width = funct3[1:0];
    wire [1:0] lane = alu_out[1:0];
    wire [3:0] strb = (width == 2'd0) ? (4'b0001 << lane) :
                      (width == 2'd1) ? (lane[1] ? 4'b1100 : 4'b0011) : 4'b1111;

    // The strobed lanes of the word read: a halfword mux on lane[1], a byte
    // mux on lane[0], then extension by funct3[2] (clear: sign, lb and lh;
    // set: zero, lbu and lhu).
    wire [15:0] load_half = lane[1] ? mdr[31:16] : mdr[15:0];
    wire [7:0] load_byte = lane[0] ? load_half[15:8] : load_half[7:0];
    wire [31:0] load_value =
        (width == 2'd0) ? {{24{load_byte[7] & ~funct3[2]}}, load_byte} :
        (width == 2'd1) ? {{16{load_half[15] & ~funct3[2]}}, load_half} : mdr;

    // CSRs: the old value is the result, captured into alu_out in EXECUTE;
    // the new value is written in WRITEBACK. The operand is rs1 or its
    // five-bit field (funct3[2]); csrrs/csrrc with a zero field write nothing.
    wire [11:0] csr_addr = ir[31:20];
    wire [31:0] csr_old = (csr_addr == CSR_MTVEC) ? mtvec : (csr_addr == CSR_MEPC) ? mepc :
                          (csr_addr == CSR_MCAUSE) ? mcause : mtval;
    wire [31:0] csr_operand = funct3[2] ? {27'd0, rs1} : a;
    wire [31:0] csr_new = (funct3[1:0] == 2'd1) ? csr_operand :
                          (funct3[1:0] == 2'd2) ? (csr_old | csr_operand) : (csr_old & ~csr_operand);
    wire csr_we = is_csr && ((funct3[1:0] == 2'd1) || (rs1 != 5'd0));

    // Register file: written in WRITEBACK, read in DECODE.
    wire [31:0] rs1_value, rs2_value;
    wire rd_written = writes_rd && (rd != 5'd0); // one x0 test for the write and the trace
    wire rf_we = (state == WRITEBACK) && rd_written;
    wire [31:0] rd_value = is_load ? load_value : (is_jal || is_jalr) ? ir_pc + 32'd4 : alu_out;

    rv32_regfile regfile (
        .clk(clk), .reset(reset), .we(rf_we), .waddr(rd), .wdata(rd_value),
        .raddr1(rs1), .raddr2(rs2), .rdata1(rs1_value), .rdata2(rs2_value));

    // The ALU sees the registers read in DECODE (zero for lui, whose result
    // is the immediate itself) and either the second register or the
    // immediate. It computes results, effective addresses, the jalr target,
    // and, for a branch, the comparison of rs1 with rs2. PC-relative targets
    // (auipc, jal, branches) come from a separate adder so the ALU's flags
    // are free for the branch decision in the same cycle.
    wire uses_alu_op = is_alu_imm || is_alu_reg;
    wire [31:0] alu_a = is_lui ? 32'd0 : a;
    wire [31:0] alu_b = (is_alu_reg || is_branch) ? b : imm;
    wire [31:0] alu_result;
    wire alu_eq, alu_lt, alu_ltu;

    rv32_alu alu (.a(alu_a), .b(alu_b), .op(uses_alu_op ? funct3 : 3'd0),
                  .alt(uses_alu_op && alu_alt), .result(alu_result),
                  .eq(alu_eq), .lt(alu_lt), .ltu(alu_ltu));

    wire [31:0] pc_target = ir_pc + imm;
    // funct3: 000 beq, 001 bne, 100 blt, 101 bge, 110 bltu, 111 bgeu;
    // bit 0 inverts, bit 2 selects order over equality, bit 1 unsigned.
    wire branch_cond = funct3[2] ? (funct3[1] ? alu_ltu : alu_lt) : alu_eq;
    wire branch_taken = is_branch && (branch_cond ^ funct3[0]);
    wire [31:0] jalr_target = {alu_result[31:1], 1'b0};
    wire [31:0] execute_out = is_csr ? csr_old : is_jalr ? jalr_target :
                              (is_auipc || is_jal || is_branch) ? pc_target : alu_result;

    wire access_misaligned = (is_load || is_store) &&
                             ((width == 2'd2 && alu_result[1:0] != 2'b00) ||
                              (width == 2'd1 && alu_result[0]));
    wire target_misaligned = (is_jal || is_jalr || branch_taken) && execute_out[1];

    // Memory port: a fetch in FETCH, a data access in MEM, nothing otherwise
    // and nothing while reset is asserted (the state register already says
    // FETCH then, so the gate is explicit).
    assign mem_valid = !reset && ((state == FETCH) || (state == MEM));
    assign mem_fetch = mem_valid && (state == FETCH);
    assign mem_addr = mem_fetch ? pc : alu_out;
    assign mem_we = mem_valid && (state == MEM) && is_store;
    assign mem_strb = !mem_valid ? 4'b0000 : mem_fetch ? 4'b1111 : strb;
    // Sub-word store data is replicated across the lanes so the strobe alone selects it.
    assign mem_wdata = (width == 2'd0) ? {4{b[7:0]}} : (width == 2'd1) ? {2{b[15:0]}} : b;

    // Trap entry: report it on the retirement port; vector through mtvec
    // unless the previous trap's handler has not retired yet, which halts
    // the core with the CSRs of the first trap intact.
    task take_trap;
        input [3:0] cause;
        input [31:0] value;
        input [31:0] epc;
        begin
            trap <= 1'b1;
            trap_cause <= cause;
            trap_value <= value;
            if (in_trap) begin
                state <= HALT;
                halted <= 1'b1;
            end else begin
                in_trap <= 1'b1;
                mepc <= epc;
                mcause <= {28'd0, cause};
                mtval <= value;
                pc <= mtvec;
                state <= FETCH;
            end
        end
    endtask

    always @(posedge clk) begin
        if (reset) begin
            state <= FETCH;
            pc <= RESET_PC;
            ir <= 32'd0;
            ir_pc <= RESET_PC;
            a <= 32'd0;
            b <= 32'd0;
            alu_out <= 32'd0;
            mdr <= 32'd0;
            taken <= 1'b0;
            in_trap <= 1'b0;
            mtvec <= 32'd0;
            mepc <= 32'd0;
            mcause <= 32'd0;
            mtval <= 32'd0;
            retire <= 1'b0;
            retire_pc <= 32'd0;
            retire_insn <= 32'd0;
            retire_rd_we <= 1'b0;
            retire_rd <= 5'd0;
            retire_rd_value <= 32'd0;
            trap <= 1'b0;
            trap_cause <= 4'd0;
            trap_value <= 32'd0;
            halted <= 1'b0;
        end else begin
            retire <= 1'b0;
            trap <= 1'b0;
            case (state)
                FETCH: if (mem_ready) begin
                    ir <= mem_rdata;
                    ir_pc <= pc;
                    retire_pc <= pc;
                    retire_insn <= mem_error ? 32'd0 : mem_rdata;
                    if (mem_error)
                        take_trap(CAUSE_FETCH_FAULT, pc, pc);
                    else
                        state <= DECODE;
                end
                DECODE: begin
                    a <= rs1_value;
                    b <= rs2_value;
                    if (illegal)
                        take_trap(CAUSE_ILLEGAL, ir, ir_pc);
                    else if (is_ecall)
                        take_trap(CAUSE_ECALL, 32'd0, ir_pc);
                    else if (is_ebreak)
                        take_trap(CAUSE_BREAKPOINT, ir_pc, ir_pc);
                    else
                        state <= EXECUTE;
                end
                EXECUTE: begin
                    alu_out <= execute_out;
                    taken <= branch_taken;
                    if (access_misaligned)
                        take_trap(is_load ? CAUSE_LOAD_MISALIGNED : CAUSE_STORE_MISALIGNED, alu_result, ir_pc);
                    else if (target_misaligned)
                        take_trap(CAUSE_TARGET_MISALIGNED, execute_out, ir_pc);
                    else if (is_load || is_store)
                        state <= MEM;
                    else
                        state <= WRITEBACK;
                end
                MEM: if (mem_ready) begin
                    mdr <= mem_rdata;
                    if (mem_error)
                        take_trap(is_load ? CAUSE_LOAD_FAULT : CAUSE_STORE_FAULT, alu_out, ir_pc);
                    else
                        state <= WRITEBACK;
                end
                WRITEBACK: begin
                    // The register file samples rf_we/rd_value on this same edge;
                    // a CSR write lands here too, so the instruction's effects commit together.
                    pc <= is_mret ? mepc : (is_jal || is_jalr || taken) ? alu_out : ir_pc + 32'd4;
                    if (csr_we) begin
                        case (csr_addr)
                            CSR_MTVEC: mtvec <= {csr_new[31:2], 2'b00}; // direct mode only
                            CSR_MEPC: mepc <= {csr_new[31:2], 2'b00};   // IALIGN is 32
                            CSR_MCAUSE: mcause <= csr_new;
                            default: mtval <= csr_new; // CSR_MTVAL: the decoder admits no other number
                        endcase
                    end
                    in_trap <= 1'b0;
                    retire <= 1'b1;
                    retire_rd_we <= rd_written;
                    retire_rd <= rd;
                    retire_rd_value <= rd_value;
                    state <= FETCH;
                end
                default: begin end // HALT: hold until reset.
            endcase
        end
    end
endmodule
