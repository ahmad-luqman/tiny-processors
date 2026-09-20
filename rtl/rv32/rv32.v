`timescale 1ns/1ps

// Multicycle RV32I slice: FETCH, DECODE, EXECUTE, MEM, WRITEBACK, one
// ready/valid memory port, retirement in the last state. The contract is
// docs/rv32-rtl.md; the datapath and controller are explained in
// docs/rv32-to-gates.md.
module rv32 (
    input  wire        clk,
    input  wire        reset,
    // Memory port (docs/rv32.md, "Memory transaction contract").
    output wire        mem_valid,
    output wire [31:0] mem_addr,
    output wire        mem_we,
    output wire [3:0]  mem_wstrb,
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
    output reg         halted,
    output reg         fault,
    output reg  [3:0]  fault_cause,
    output reg  [31:0] fault_value,
    output reg         unsupported,
    output reg  [2:0]  state,
    output reg  [31:0] pc
);
    localparam [2:0] FETCH = 3'd0, DECODE = 3'd1, EXECUTE = 3'd2,
                     MEM = 3'd3, WRITEBACK = 3'd4, HALT = 3'd5;
    localparam [3:0] CAUSE_TARGET_MISALIGNED = 4'd0, CAUSE_FETCH_FAULT = 4'd1,
                     CAUSE_ILLEGAL = 4'd2, CAUSE_BREAKPOINT = 4'd3,
                     CAUSE_LOAD_MISALIGNED = 4'd4, CAUSE_LOAD_FAULT = 4'd5,
                     CAUSE_STORE_MISALIGNED = 4'd6, CAUSE_STORE_FAULT = 4'd7,
                     CAUSE_ECALL = 4'd11;
    localparam [31:0] RESET_PC = 32'h8000_0000;

    // Datapath registers: one instruction's worth of state between states.
    reg [31:0] ir, ir_pc, a, b, alu_out, mdr;
    reg taken;

    // Decoded fields, combinational from ir.
    wire [4:0] rd, rs1, rs2;
    wire [31:0] imm;
    wire is_lui, is_auipc, is_alu_reg, alu_sub, is_load, is_store, store_byte;
    wire is_branch, branch_ne, is_jal, is_ecall, is_ebreak, writes_rd, illegal, unsupported_insn;

    rv32_decode decode (
        .insn(ir), .rd(rd), .rs1(rs1), .rs2(rs2), .imm(imm),
        .is_lui(is_lui), .is_auipc(is_auipc), .is_alu_reg(is_alu_reg),
        .alu_sub(alu_sub), .is_load(is_load), .is_store(is_store), .store_byte(store_byte),
        .is_branch(is_branch), .branch_ne(branch_ne), .is_jal(is_jal),
        .is_ecall(is_ecall), .is_ebreak(is_ebreak), .writes_rd(writes_rd),
        .illegal(illegal), .unsupported(unsupported_insn));

    // Register file: written in WRITEBACK, read in DECODE.
    wire [31:0] rs1_value, rs2_value;
    wire rd_written = writes_rd && (rd != 5'd0); // one x0 test for the write and the trace
    wire rf_we = (state == WRITEBACK) && rd_written;
    wire [31:0] rd_value = is_load ? mdr : is_jal ? ir_pc + 32'd4 : alu_out;

    rv32_regfile regfile (
        .clk(clk), .reset(reset), .we(rf_we), .waddr(rd), .wdata(rd_value),
        .raddr1(rs1), .raddr2(rs2), .rdata1(rs1_value), .rdata2(rs2_value));

    // ALU operand selection: the PC for auipc/branches/jal, zero for lui,
    // otherwise the register read in DECODE; the second operand is a register
    // only for add/sub.
    wire uses_pc = is_auipc || is_branch || is_jal;
    wire [31:0] alu_a = uses_pc ? ir_pc : is_lui ? 32'd0 : a;
    wire [31:0] alu_b = is_alu_reg ? b : imm;
    wire [31:0] alu_result;

    rv32_alu alu (.a(alu_a), .b(alu_b), .sub(is_alu_reg && alu_sub), .result(alu_result));

    wire rs_equal = (a == b);
    wire branch_taken = is_branch && (rs_equal ^ branch_ne);
    wire word_access = is_load || (is_store && !store_byte);
    wire access_misaligned = word_access && (alu_result[1:0] != 2'b00);
    wire target_misaligned = (is_jal || branch_taken) && alu_result[1];

    // Memory port: a fetch in FETCH, a data access in MEM, nothing otherwise
    // and nothing while reset is asserted (the state register already says
    // FETCH then, so the gate is explicit).
    assign mem_valid = !reset && ((state == FETCH) || (state == MEM));
    assign mem_fetch = mem_valid && (state == FETCH);
    assign mem_addr = mem_fetch ? pc : alu_out;
    assign mem_we = mem_valid && (state == MEM) && is_store;
    assign mem_wstrb = !mem_we ? 4'b0000 :
                       !store_byte ? 4'b1111 :
                       (4'b0001 << alu_out[1:0]);
    assign mem_wdata = store_byte ? {4{b[7:0]}} : b;

    task stop;
        input is_fault;
        input [3:0] cause;
        input [31:0] value;
        begin
            state <= HALT;
            halted <= 1'b1;
            fault <= is_fault;
            unsupported <= !is_fault;
            fault_cause <= cause;
            fault_value <= value;
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
            retire <= 1'b0;
            retire_pc <= 32'd0;
            retire_insn <= 32'd0;
            retire_rd_we <= 1'b0;
            retire_rd <= 5'd0;
            retire_rd_value <= 32'd0;
            halted <= 1'b0;
            fault <= 1'b0;
            fault_cause <= 4'd0;
            fault_value <= 32'd0;
            unsupported <= 1'b0;
        end else begin
            retire <= 1'b0;
            case (state)
                FETCH: if (mem_ready) begin
                    ir <= mem_rdata;
                    ir_pc <= pc;
                    retire_pc <= pc;
                    retire_insn <= mem_error ? 32'd0 : mem_rdata;
                    if (mem_error)
                        stop(1'b1, CAUSE_FETCH_FAULT, pc);
                    else
                        state <= DECODE;
                end
                DECODE: begin
                    a <= rs1_value;
                    b <= rs2_value;
                    if (illegal)
                        stop(1'b1, CAUSE_ILLEGAL, ir);
                    else if (is_ecall)
                        stop(1'b1, CAUSE_ECALL, 32'd0);
                    else if (is_ebreak)
                        stop(1'b1, CAUSE_BREAKPOINT, ir_pc);
                    else if (unsupported_insn)
                        stop(1'b0, 4'd0, 32'd0);
                    else
                        state <= EXECUTE;
                end
                EXECUTE: begin
                    alu_out <= alu_result;
                    taken <= branch_taken;
                    if (access_misaligned)
                        stop(1'b1, is_load ? CAUSE_LOAD_MISALIGNED : CAUSE_STORE_MISALIGNED, alu_result);
                    else if (target_misaligned)
                        stop(1'b1, CAUSE_TARGET_MISALIGNED, alu_result);
                    else if (is_load || is_store)
                        state <= MEM;
                    else
                        state <= WRITEBACK;
                end
                MEM: if (mem_ready) begin
                    mdr <= mem_rdata;
                    if (mem_error)
                        stop(1'b1, is_load ? CAUSE_LOAD_FAULT : CAUSE_STORE_FAULT, alu_out);
                    else
                        state <= WRITEBACK;
                end
                WRITEBACK: begin
                    // The register file samples rf_we/rd_value on this same edge.
                    pc <= (is_jal || taken) ? alu_out : ir_pc + 32'd4;
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
