`timescale 1ns/1ps

// Multicycle RV32IMAF core: the integer path plus FP_ISSUE/FP_WAIT, the M
// extension's MD_WAIT and (issue #34) the A extension's AMO_WRITE. One
// ready/valid memory port, the machine and (issue #20) supervisor trap CSRs
// with an Sv32 page-table walker and (issue #24) a 4-entry TLB, floating
// CSRs, the Zicntr counters, and atomic register/flag retirement in
// WRITEBACK. See docs/rv32-f.md, docs/rv32-groundwork.md and docs/rv32-a.md. A trap vectors to mtvec, or to
// stvec when delegated; a second trap before the handler retires an instruction halts the core
// (the emulator's double-fault rule). The contract is docs/rv32-rtl.md;
// the datapath and controller are explained in docs/rv32-to-gates.md.
module rv32 #(
    // Boot convention (docs/rv32.md, "Reset"): a0 holds the hart id and a1 the
    // address of the machine's device tree when the first instruction runs.
    parameter [31:0] BOOT_A1 = 32'h0000_1000
) (
    input  wire        clk,
    input  wire        reset,
    // The platform's mtime (the CLINT's count), which the `time` CSR shadows.
    input  wire [63:0] time_now,
    // Interrupt levels (O1): mip.MSIP and mip.MTIP from the CLINT, mip.MEIP from the PLIC.
    input  wire        irq_software,
    input  wire        irq_timer,
    input  wire        irq_external,
    // Deterministic tick mode (O1): `cycle` counts steps, not clock cycles, and wfi never waits.
    input  wire        step_ticks,
    // Issue #34: a write to RAM by a device (DMA), not by this core, accepted this cycle; a write
    // to LR.W's reserved physical word ends the reservation.
    input  wire        ram_snoop_write,
    input  wire [31:0] ram_snoop_addr,
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
    output wire        mem_ptw,   // issue #20: a page-table read, which only RAM may answer
    // Retirement port for the testbench.
    output reg         retire,
    output reg  [31:0] retire_pc,
    output reg  [31:0] retire_insn,
    output reg         retire_rd_we,
    output reg  [4:0]  retire_rd,
    output reg  [31:0] retire_rd_value,
    output reg         retire_fd_we,
    output reg [4:0]   retire_fd,
    output reg [31:0]  retire_fd_value,
    output reg         retire_fcsr_we,
    output reg [7:0]   retire_fcsr,
    output reg         trap,
    output reg         trap_interrupt, // the trap was an interrupt entry: no instruction, trap_cause is its code
    output reg  [3:0]  trap_cause,
    output reg  [31:0] trap_value,
    output reg         halted,
    output reg  [3:0]  state,
    output reg  [31:0] pc,
    output reg  [31:0] mtvec,
    output reg  [31:0] mepc,
    output reg  [31:0] mcause,
    output reg  [31:0] mtval
);
    localparam [3:0] FETCH = 4'd0, DECODE = 4'd1, EXECUTE = 4'd2,
                     MEM = 4'd3, WRITEBACK = 4'd4, HALT = 4'd5, FP_ISSUE = 4'd6, FP_WAIT = 4'd7,
                     MD_WAIT = 4'd8, WFI_WAIT = 4'd9,
                     WALK = 4'd10, XLATE = 4'd11, // issue #20: a page-table read; PMP on a translated data address
                     AMO_WRITE = 4'd12;           // issue #34: an AMO's write, after its read in MEM
    localparam [3:0] CAUSE_TARGET_MISALIGNED = 4'd0, CAUSE_FETCH_FAULT = 4'd1,
                     CAUSE_ILLEGAL = 4'd2, CAUSE_BREAKPOINT = 4'd3,
                     CAUSE_LOAD_MISALIGNED = 4'd4, CAUSE_LOAD_FAULT = 4'd5,
                     CAUSE_STORE_MISALIGNED = 4'd6, CAUSE_STORE_FAULT = 4'd7,
                     CAUSE_ECALL_U = 4'd8, CAUSE_ECALL_S = 4'd9, CAUSE_ECALL = 4'd11,
                     CAUSE_FETCH_PAGE = 4'd12, CAUSE_LOAD_PAGE = 4'd13, CAUSE_STORE_PAGE = 4'd15;
    localparam [1:0] PRIV_U = 2'd0, PRIV_S = 2'd1, PRIV_M = 2'd3;
    localparam [11:0] CSR_MTVEC = 12'h305, CSR_MEPC = 12'h341, CSR_MCAUSE = 12'h342,
                      CSR_MTVAL = 12'h343, CSR_MSTATUS = 12'h300, CSR_MIE = 12'h304, CSR_MSCRATCH = 12'h340,
                      CSR_MIP = 12'h344, CSR_MCOUNTEREN = 12'h306, CSR_PMPCFG0 = 12'h3a0, CSR_PMPCFG1 = 12'h3a1,
                      CSR_FFLAGS = 12'h001, CSR_FRM = 12'h002, CSR_FCSR = 12'h003,
                      CSR_CYCLE = 12'hc00, CSR_TIME = 12'hc01, CSR_INSTRET = 12'hc02,
                      CSR_CYCLEH = 12'hc80, CSR_TIMEH = 12'hc81, CSR_INSTRETH = 12'hc82,
                      CSR_MEDELEG = 12'h302, CSR_MIDELEG = 12'h303, CSR_SSTATUS = 12'h100, CSR_SIE = 12'h104,
                      CSR_STVEC = 12'h105, CSR_SCOUNTEREN = 12'h106, CSR_SSCRATCH = 12'h140, CSR_SEPC = 12'h141,
                      CSR_SCAUSE = 12'h142, CSR_STVAL = 12'h143, CSR_SIP = 12'h144, CSR_SATP = 12'h180;
    localparam [2:0] DIRECT_NONE=3'd0, DIRECT_SIGN=3'd1, DIRECT_CLASS=3'd3;
    localparam [31:0] RESET_PC = 32'h8000_0000;

    // Datapath registers: one instruction's worth of state between states.
    reg [31:0] ir, ir_pc, a, b, alu_out, mdr;
    reg [7:0] fcsr;
    reg [31:0] fa, fb, fc;
    reg [4:0] fp_flags;
    reg taken;
    reg in_trap; // a trap was taken and its handler has not retired an instruction yet
    // Zicntr: clock cycles since reset and instructions retired since reset.
    // `time` reads the CLINT's mtime (time_now), as on QEMU virt, so a write
    // to mtime moves it (docs/rv32.md, "CLINT").
    reg [63:0] cycle_count, instret_count;
    // Interrupts (O1): mstatus.MIE/MPIE, the three enables of mie (MSIE, MTIE, MEIE), mscratch.
    reg mstatus_mie, mstatus_mpie;
    reg [2:0] mie_bits; // {MEIE, MTIE, MSIE}
    reg [31:0] mscratch;
    reg fetch_waiting;  // this FETCH has presented its request, so it can no longer be replaced
    // Protection (O5): the privilege mode and mstatus.MPP (3 machine, 1 supervisor since issue #20,
    // 0 user), the counters user mode may read, and eight PMP entries.
    reg [1:0] priv, mpp;
    wire priv_m = priv == PRIV_M;
    reg [2:0] mcounteren;
    // S-mode (issue #20): mstatus's supervisor fields, delegation, the supervisor's interrupt
    // enables and the software-raised supervisor interrupts ({SEI, STI, SSI} in each), the
    // supervisor's trap CSRs and satp (MODE and a 22-bit PPN; the ASID is 0 bits wide).
    reg mstatus_sie, mstatus_spie, mstatus_spp, mstatus_mprv, mstatus_sum, mstatus_mxr;
    reg mstatus_tvm, mstatus_tw, mstatus_tsr;
    // Floating state (issue #33): mstatus.FS, Off 0, Initial 1, Clean 2, Dirty 3. Dirty from reset,
    // so F firmware that never writes it runs; Off makes the F instructions and CSRs illegal.
    reg [1:0] mstatus_fs;
    reg [15:0] medeleg;
    reg [2:0] mideleg, mie_s, mip_soft, scounteren;
    reg [31:0] stvec, sscratch, sepc, scause, stval;
    reg satp_mode;
    reg [21:0] satp_ppn;
    // Sv32 (issue #20): the page-table walk. `phys` holds the translation of the current fetch or
    // data access once `xlate_ok` is set; `pte_addr` is the entry the walk reads next.
    localparam [1:0] WALK_FETCH = 2'd0, WALK_LOAD = 2'd1, WALK_STORE = 2'd2;
    reg xlate_ok, walk_level;
    reg [1:0] walk_kind;
    reg [31:0] phys;
    reg [33:0] pte_addr;
    // The TLB (issue #24): four fully associative entries, each a leaf that a walk accepted. An
    // entry holds its virtual page (VPN[1] alone counts for a megapage), the physical page (bits
    // 31:12: a leaf at or past 2^32 is never filled) and the leaf's {D, U, X, W, R}. A is not kept,
    // because a walk never accepts a leaf with A clear. The ASID is 0 bits wide, so the tag is the
    // VPN alone. Entries fill in turn, so the oldest goes first. sfence.vma and any write to satp
    // empty the TLB, and the fill pointer starts over at entry 0.
    reg [3:0] tlb_valid, tlb_mega;
    reg [19:0] tlb_vpn [0:3];
    reg [19:0] tlb_ppn [0:3];
    reg [4:0] tlb_perm [0:3];
    reg [1:0] tlb_next;
    reg [7:0] pmpcfg [0:7];
    reg [31:0] pmpaddr [0:7];
    // The A extension (issue #34): LR.W's reservation. SC.W compares the virtual word, so a failing
    // SC needs no translation; a device's write to the physical word ends it, as do SC.W, any trap,
    // mret, sret, a satp write and sfence.vma. sc_held is the SC's outcome, decided in EXECUTE.
    reg reserved, sc_held;
    reg [29:0] reservation, reservation_pa;

    // Decoded fields, combinational from ir.
    wire [4:0] rd, rs1, rs2;
    wire [2:0] funct3;
    wire [31:0] imm;
    wire is_lui, is_auipc, is_alu_imm, is_alu_reg, is_muldiv, alu_alt, is_load, is_store, is_lr, is_sc, is_amo;
    wire is_branch, is_jal, is_jalr, is_csr, is_mret, is_ecall, is_ebreak, is_wfi, is_sret, is_sfence, writes_rd, illegal;

    rv32_decode decode (
        .insn(ir), .rd(rd), .rs1(rs1), .rs2(rs2), .funct3(funct3), .imm(imm),
        .is_lui(is_lui), .is_auipc(is_auipc), .is_alu_imm(is_alu_imm), .is_alu_reg(is_alu_reg), .is_muldiv(is_muldiv),
        .alu_alt(alu_alt), .is_load(is_load), .is_store(is_store), .is_lr(is_lr), .is_sc(is_sc), .is_amo(is_amo),
        .is_branch(is_branch),
        .is_jal(is_jal), .is_jalr(is_jalr), .is_csr(is_csr), .is_mret(is_mret),
        .is_ecall(is_ecall), .is_ebreak(is_ebreak), .is_wfi(is_wfi), .is_sret(is_sret), .is_sfence(is_sfence),
        .writes_rd(writes_rd), .illegal(illegal));

    // mip is the live levels, and since issue #20 the supervisor interrupts machine mode raises.
    // An interrupt is taken in place of a fetch that has not been presented yet, so a request on
    // the bus is never withdrawn, and never before a handler's first instruction retires, so it
    // cannot make a double fault of a trap into S mode. Those bound for machine mode come before
    // those mideleg sends to S mode, and within one mode the order is MEI, MSI, MTI, SEI, SSI, STI.
    // One bound for machine mode is enabled below it whatever MIE says (O5); a delegated one is
    // enabled below S mode, or in it with SIE, and never in machine mode.
    wire [31:0] mip = {20'd0, irq_external, 1'b0, mip_soft[2], 1'b0, irq_timer, 1'b0, mip_soft[1], 1'b0,
                       irq_software, 1'b0, mip_soft[0], 1'b0};
    wire [31:0] mie_value = {20'd0, mie_bits[2], 1'b0, mie_s[2], 1'b0, mie_bits[1], 1'b0, mie_s[1], 1'b0,
                             mie_bits[0], 1'b0, mie_s[0], 1'b0};
    wire [31:0] mideleg_value = {22'd0, mideleg[2], 3'd0, mideleg[1], 3'd0, mideleg[0], 1'b0};
    wire [2:0] irq_ready = {irq_external, irq_timer, irq_software} & mie_bits; // {MEI, MTI, MSI}
    wire [2:0] irq_ready_s = mip_soft & mie_s;                                  // {SEI, STI, SSI}
    wire irq_wake = (irq_ready != 3'd0) || (irq_ready_s != 3'd0);              // what ends a wfi
    wire m_enabled = !priv_m || mstatus_mie;
    wire s_enabled = priv == PRIV_U || (priv == PRIV_S && mstatus_sie);
    wire [2:0] take_m = m_enabled ? irq_ready : 3'd0;
    wire [2:0] take_sm = m_enabled ? irq_ready_s & ~mideleg : 3'd0;  // S interrupts bound for M
    wire [2:0] take_ss = s_enabled ? irq_ready_s & mideleg : 3'd0;   // and those delegated to S
    wire [2:0] take_s = (take_m != 3'd0 || take_sm != 3'd0) ? take_sm : take_ss;
    wire irq_take = (state == FETCH) && !fetch_waiting && !in_trap && (take_m != 3'd0 || take_s != 3'd0);
    wire [3:0] irq_code = take_m[2] ? 4'd11 : take_m[0] ? 4'd3 : take_m[1] ? 4'd7 :
                          take_s[2] ? 4'd9 : take_s[0] ? 4'd1 : 4'd5;

    wire fp_valid, fp_to_integer, fp_from_integer;
    wire [4:0] fp_op;
    wire [2:0] fp_rm, fp_direct;
    wire fp_load = is_load && ir[6:0] == 7'h07;
    wire fp_store = is_store && ir[6:0] == 7'h27;
    wire fp_write = fp_load || (fp_valid && !fp_to_integer);
    wire [31:0] f1, f2, f3;
    wire [31:0] fp_value = fp_load ? mdr : alu_out;
    rv32_fdecode fdecode (.insn(ir), .frm(fcsr[7:5]), .valid(fp_valid),
        .op(fp_op), .rm(fp_rm), .to_integer(fp_to_integer),
        .from_integer(fp_from_integer), .direct(fp_direct));
    rv32_fregfile fregfile (.clk(clk), .reset(reset),
        .we(state == WRITEBACK && fp_write), .waddr(rd), .wdata(fp_value),
        .raddr1(rs1), .raddr2(rs2), .raddr3(ir[31:27]), .rdata1(f1), .rdata2(f2), .rdata3(f3));
    wire fp_ready, fp_done, fp_error;
    wire [31:0] fp_result;
    wire [4:0] fp_result_flags;
    fp32 fpu (.clk(clk), .reset(reset), .req_valid(!reset && state == FP_ISSUE),
        .req_ready(fp_ready), .op(fp_op), .rm(fp_rm), .a(fa), .b(fb), .c(fc),
        .resp_valid(fp_done), .resp_ready(!reset && state == FP_WAIT),
        .result(fp_result), .flags(fp_result_flags), .error(fp_error));

    // The M extension: started from EXECUTE with the operands read in DECODE,
    // finished 33 MD_WAIT cycles later (rtl/rv32/rv32_muldiv.v). md_start is
    // also the EXECUTE arm that enters MD_WAIT: an M instruction is never a
    // load, store, jump, branch or FP operation, so no earlier arm can take it.
    wire md_start = (state == EXECUTE) && is_muldiv;
    wire md_valid;
    wire [31:0] md_result;
    rv32_muldiv muldiv (.clk(clk), .reset(reset), .start(md_start),
        .funct3(funct3), .a(a), .b(b), .valid(md_valid), .result(md_result));

    // Classification uses exponent/fraction detectors, never arithmetic.
    wire f_zero_exp = fa[30:23] == 8'd0;
    wire f_max_exp = fa[30:23] == 8'hff;
    wire f_zero_frac = fa[22:0] == 23'd0;
    wire [9:0] classification = {
        f_max_exp && !f_zero_frac && fa[22],
        f_max_exp && !f_zero_frac && !fa[22],
        !fa[31] && f_max_exp && f_zero_frac,
        !fa[31] && !f_zero_exp && !f_max_exp,
        !fa[31] && f_zero_exp && !f_zero_frac,
        !fa[31] && f_zero_exp && f_zero_frac,
        fa[31] && f_zero_exp && f_zero_frac,
        fa[31] && f_zero_exp && !f_zero_frac,
        fa[31] && !f_zero_exp && !f_max_exp,
        fa[31] && f_max_exp && f_zero_frac};
    wire injected_sign = funct3 == 3'd0 ? fb[31] : funct3 == 3'd1 ? !fb[31] : fa[31] ^ fb[31];
    wire [31:0] direct_result = fp_direct == DIRECT_SIGN ? {injected_sign, fa[30:0]} :
                               fp_direct == DIRECT_CLASS ? {22'd0, classification} : fa;


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

    // mstatus: SD (FS Dirty), FS, MPP, and the fields S-mode added.
    wire [31:0] mstatus_value = {&mstatus_fs, 8'd0, mstatus_tsr, mstatus_tw, mstatus_tvm, mstatus_mxr, mstatus_sum, mstatus_mprv,
                                 2'b00, mstatus_fs, mpp, 2'b00, mstatus_spp, mstatus_mpie, 1'b0, mstatus_spie, 1'b0,
                                 mstatus_mie, 1'b0, mstatus_sie, 1'b0};
    wire [31:0] sstatus_value = mstatus_value & 32'h800c_6122; // SD, MXR, SUM, FS, SPP, SPIE, SIE

    // CSRs: the old value is the result, captured into alu_out in EXECUTE;
    // the new value is written in WRITEBACK. The operand is rs1 or its
    // five-bit field (funct3[2]); csrrs/csrrc with a zero field write nothing.
    wire [11:0] csr_addr = ir[31:20];
    // One arm per CSR; decode makes every other number illegal, so the last
    // arm is mtval. (An `always @* case` form of this reads the same but
    // synthesizes to a parallel mux about 150 generic cells larger.)
    wire [31:0] csr_old =
        (csr_addr == CSR_FFLAGS) ? {27'd0, fcsr[4:0]} :
        (csr_addr == CSR_FRM) ? {29'd0, fcsr[7:5]} :
        (csr_addr == CSR_FCSR) ? {24'd0, fcsr} :
        (csr_addr == CSR_MTVEC) ? mtvec :
        (csr_addr == CSR_MEPC) ? mepc :
        (csr_addr == CSR_MCAUSE) ? mcause :
        (csr_addr == CSR_MSTATUS) ? mstatus_value :
        (csr_addr == CSR_SSTATUS) ? sstatus_value :
        (csr_addr == CSR_MEDELEG) ? {16'd0, medeleg} :
        (csr_addr == CSR_MIDELEG) ? mideleg_value :
        (csr_addr == CSR_SIE) ? (mie_value & mideleg_value) :
        (csr_addr == CSR_SIP) ? (mip & mideleg_value) :
        (csr_addr == CSR_STVEC) ? stvec :
        (csr_addr == CSR_SCOUNTEREN) ? {29'd0, scounteren} :
        (csr_addr == CSR_SSCRATCH) ? sscratch :
        (csr_addr == CSR_SEPC) ? sepc :
        (csr_addr == CSR_SCAUSE) ? scause :
        (csr_addr == CSR_STVAL) ? stval :
        (csr_addr == CSR_SATP) ? {satp_mode, 9'd0, satp_ppn} :
        (csr_addr == CSR_MCOUNTEREN) ? {29'd0, mcounteren} :
        (csr_addr == CSR_PMPCFG0) ? {pmpcfg[3], pmpcfg[2], pmpcfg[1], pmpcfg[0]} :
        (csr_addr == CSR_PMPCFG1) ? {pmpcfg[7], pmpcfg[6], pmpcfg[5], pmpcfg[4]} :
        (csr_addr[11:3] == 9'h076) ? pmpaddr[csr_addr[2:0]] :
        (csr_addr == CSR_MIE) ? mie_value :
        (csr_addr == CSR_MIP) ? mip :
        (csr_addr == CSR_MSCRATCH) ? mscratch :
        (csr_addr == CSR_CYCLE) ? cycle_count[31:0] :
        (csr_addr == CSR_CYCLEH) ? cycle_count[63:32] :
        (csr_addr == CSR_TIME) ? time_now[31:0] :
        (csr_addr == CSR_TIMEH) ? time_now[63:32] :
        (csr_addr == CSR_INSTRET) ? instret_count[31:0] :
        (csr_addr == CSR_INSTRETH) ? instret_count[63:32] :
        mtval; // CSR_MTVAL
    wire [31:0] csr_operand = funct3[2] ? {27'd0, rs1} : a;
    wire [31:0] csr_new = (funct3[1:0] == 2'd1) ? csr_operand :
                          (funct3[1:0] == 2'd2) ? (csr_old | csr_operand) : (csr_old & ~csr_operand);
    wire [2:0] csr_new_s = {csr_new[9], csr_new[5], csr_new[1]}; // SEI, STI, SSI as mideleg orders them
    wire csr_we = is_csr && ((funct3[1:0] == 2'd1) || (rs1 != 5'd0));

    wire fp_csr_write = csr_we && csr_addr >= CSR_FFLAGS && csr_addr <= CSR_FCSR;
    wire [7:0] fp_csr_new = csr_addr == CSR_FFLAGS ? {fcsr[7:5], csr_new[4:0]} :
                              csr_addr == CSR_FRM ? {csr_new[2:0], fcsr[4:0]} : csr_new[7:0];

    wire [7:0] fcsr_next = fp_csr_write ? fp_csr_new : fcsr | {3'd0, fp_flags};

    // Register file: written in WRITEBACK, read in DECODE.
    wire [31:0] rs1_value, rs2_value;
    wire rd_written = (writes_rd || (fp_valid && fp_to_integer)) && (rd != 5'd0); // one x0 test for the write and the trace
    wire rf_we = (state == WRITEBACK) && rd_written;
    // An AMO returns the word it read; SC.W returns 0 when it stored, 1 when it failed.
    wire [31:0] rd_value = is_load ? load_value : is_amo ? mdr : is_sc ? {31'd0, !sc_held} :
                           (is_jal || is_jalr) ? ir_pc + 32'd4 : alu_out;

    rv32_regfile #(.RESET_A1(BOOT_A1)) regfile (
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
    // What a data access does to memory (issue #34): LR.W reads like a load, SC.W writes like a
    // store, an AMO reads and then writes. PMP, the bus and the walk's kind follow these; the
    // walk and the fault causes take `is_store`, which is mem_writes.
    wire mem_reads = is_load || is_amo;
    wire mem_writes = is_store;

    // The A extension (issue #34). SC.W without the reservation of its word skips every access,
    // translation and PMP included, and fails; its alignment is still checked first, as on QEMU.
    // An AMO's write value comes from the word MEM read (mdr) and rs2: funct5 bit 4 selects min or
    // max, bit 3 unsigned, bit 2 max; below that, bit 0 is swap and bits 3:2 add, xor, or, and
    // (funct5 is ir[31:27]; bit 1 is set only for LR and SC, which are not AMOs, so the ALU never
    // looks at it).
    wire sc_skip = is_sc && !(reserved && reservation == alu_result[31:2]);
    wire amo_minmax = ir[31], amo_unsigned = ir[30], amo_max = ir[29], amo_swap = ir[27];
    wire [1:0] amo_logic = ir[30:29];
    wire amo_less = amo_unsigned ? mdr < b : $signed(mdr) < $signed(b);
    wire [31:0] amo_value = amo_minmax ? ((amo_less ^ amo_max) ? mdr : b) :
                            amo_swap ? b :
                            amo_logic == 2'd0 ? mdr + b :
                            amo_logic == 2'd1 ? mdr ^ b :
                            amo_logic == 2'd2 ? mdr | b : mdr & b;

    // Memory port: a fetch in FETCH (at the TLB's translation on a hit, at `phys` after a walk), a
    // page-table read in WALK, a data access in MEM, an AMO's write in AMO_WRITE (issue #34),
    // nothing otherwise and nothing while reset is asserted (the state register already says
    // FETCH then, so the gate is explicit).
    // Sv32 (issue #20): translation applies to fetches below machine mode, and to loads and stores
    // below it at their effective privilege, which MPRV makes MPP's in machine mode.
    wire [1:0] data_priv = (priv_m && mstatus_mprv) ? mpp : priv;
    wire translate_fetch = satp_mode && !priv_m;
    wire translate_data = satp_mode && data_priv != PRIV_M;
    wire [31:0] walk_va = walk_kind == WALK_FETCH ? pc : alu_out;

    // The TLB lookup (issue #24): one port, on the pc in FETCH and on the effective address in
    // EXECUTE. When two entries match (only possible after a page-table change without sfence.vma),
    // the lowest-numbered one wins.
    wire [31:0] tlb_va = state == FETCH ? pc : alu_result;
    reg tlb_hit, tlb_hit_mega;
    reg [19:0] tlb_page;
    reg [4:0] tlb_bits;
    integer way;
    always @* begin
        tlb_hit = 1'b0;
        tlb_hit_mega = 1'b0;
        tlb_page = 20'd0;
        tlb_bits = 5'd0;
        for (way = 3; way >= 0; way = way - 1)
            if (tlb_valid[way] && tlb_vpn[way][19:10] == tlb_va[31:22] &&
                (tlb_mega[way] || tlb_vpn[way][9:0] == tlb_va[21:12])) begin
                tlb_hit = 1'b1;
                tlb_hit_mega = tlb_mega[way];
                tlb_page = tlb_ppn[way];
                tlb_bits = tlb_perm[way];
            end
    end
    wire [31:0] tlb_phys = {tlb_page[19:10], tlb_hit_mega ? tlb_va[21:12] : tlb_page[9:0], tlb_va[11:0]};

    // A leaf's permission against one access, shared by the walk (issue #20) and a TLB hit (issue
    // #24). U is checked against the privilege (and SUM). A fetch needs X, a store W, and a load R,
    // or X with MXR. A store also needs D (Svade: hardware never sets it). The walk checks A itself.
    function leaf_denies;
        input [4:0] bits; // {D, U, X, W, R}
        input [1:0] kind;
        input [1:0] eff_priv;
        input sum, mxr;
        begin
            leaf_denies = (eff_priv == PRIV_U ? !bits[3] : bits[3] && (kind == WALK_FETCH || !sum)) ||
                          (kind == WALK_FETCH ? !bits[2] : kind == WALK_STORE ? !bits[1] : !(bits[0] || (mxr && bits[2]))) ||
                          (kind == WALK_STORE && !bits[4]);
        end
    endfunction

    // A translated fetch looks the pc up first: a hit fetches from its physical address in the same
    // cycle (or page-faults there), and a miss walks, then fetches from `phys`.
    wire fetch_translating = (state == FETCH) && translate_fetch && !xlate_ok;
    wire fetch_walk = fetch_translating && !tlb_hit; // a fetch that needs its walk first
    wire fetch_page_fault = fetch_translating && tlb_hit && leaf_denies(tlb_bits, WALK_FETCH, priv, mstatus_sum, mstatus_mxr);
    // A translated load or store looks its address up in EXECUTE: a hit goes on to PMP and MEM in
    // the same cycle (or page-faults there), and a miss walks, then checks PMP in XLATE.
    wire data_translating = (state == EXECUTE) && (is_load || is_store) && !sc_skip && translate_data && !access_misaligned;
    wire data_page_fault = data_translating && tlb_hit &&
                           leaf_denies(tlb_bits, is_store ? WALK_STORE : WALK_LOAD, data_priv, mstatus_sum, mstatus_mxr);
    // The address a fetch presents and PMP checks: `phys` after a walk (XLATE included), the TLB's
    // translation on a hit, else the address itself (the pc in FETCH, the effective address in
    // EXECUTE). xlate_ok is never set in EXECUTE.
    wire tlb_translating = state == FETCH ? translate_fetch : translate_data;
    wire [31:0] access_addr = xlate_ok ? phys : tlb_translating ? tlb_phys : tlb_va;

    // PMP (O5): one checker, on the fetch address in FETCH and on the data address in EXECUTE; since
    // issue #20 also on a page-table read in WALK (as a supervisor read) and on a translated data
    // address in XLATE, and since issue #24 on a TLB hit's physical address in FETCH or
    // EXECUTE. The lowest-numbered matching entry decides; below machine mode an access
    // needs a match that grants it, machine mode only a locked entry's permission. Regions are
    // whole words (granularity 4).
    wire pmp_fetch = state == FETCH;
    wire [31:0] pmp_addr = state == WALK ? pte_addr[31:0] : access_addr;
    wire [31:0] pmp_word = {2'b00, pmp_addr[31:2]};
    wire [1:0] pmp_priv = pmp_fetch ? priv : state == WALK ? PRIV_S : data_priv;
    // X, W, R: a data access needs W if it writes and R if it reads, an AMO both (issue #34)
    wire [2:0] pmp_need = pmp_fetch ? 3'b100 : state == WALK ? 3'b001 : {1'b0, mem_writes, mem_reads};
    reg pmp_ok, pmp_found, pmp_match;
    reg [31:0] pmp_low, pmp_ones;
    integer entry;
    always @* begin
        pmp_ok = pmp_priv == PRIV_M;
        pmp_found = 1'b0;
        for (entry = 0; entry < 8; entry = entry + 1) begin
            pmp_low = entry == 0 ? 32'd0 : pmpaddr[entry == 0 ? 0 : entry - 1];
            pmp_ones = pmpaddr[entry] ^ (pmpaddr[entry] + 32'd1);
            case (pmpcfg[entry][4:3])
                2'd1: pmp_match = pmp_word >= pmp_low && pmp_word < pmpaddr[entry]; // TOR
                2'd2: pmp_match = pmp_word == pmpaddr[entry];                        // NA4
                2'd3: pmp_match = ((pmp_word ^ pmpaddr[entry]) & ~pmp_ones) == 32'd0; // NAPOT
                default: pmp_match = 1'b0;                                           // OFF
            endcase
            if (pmp_match && !pmp_found) begin
                pmp_found = 1'b1;
                pmp_ok = (pmp_priv == PRIV_M && !pmpcfg[entry][7]) || ((pmpcfg[entry][2:0] & pmp_need) == pmp_need);
            end
        end
    end
    wire fetch_deny = pmp_fetch && !fetch_walk && !pmp_ok; // FETCH takes fetch_page_fault first
    // A CSR needs the privilege its number's bits 9:8 name: user mode the floating CSRs and, as
    // mcounteren and scounteren allow, the counters (the only CSRs numbered 0xCxx); supervisor mode
    // also the S CSRs, the counters as mcounteren allows, and satp unless mstatus.TVM. mret below
    // machine mode, and sret, wfi and sfence.vma in user mode (or in S mode with TSR, TW, TVM), are
    // illegal instructions. Counters 0xc03 and 0xc83 do not exist (the decoder makes them illegal
    // anyway); the fourth bit is 0 so the index stays inside the vector.
    wire [3:0] counter_enable = {1'b0, mcounteren};
    wire [3:0] scounter_enable = {1'b0, scounteren};
    wire counter_ok = csr_addr[11:10] != 2'b11 ||
                      (counter_enable[csr_addr[1:0]] && (priv == PRIV_S || scounter_enable[csr_addr[1:0]]));
    wire csr_priv_ok = csr_addr[9:8] <= priv && counter_ok && !(csr_addr == CSR_SATP && mstatus_tvm);
    wire priv_illegal = !priv_m && ((is_csr && !csr_priv_ok) || is_mret ||
                                    (is_sret && (priv == PRIV_U || mstatus_tsr)) ||
                                    (is_wfi && (priv == PRIV_U || mstatus_tw)) ||
                                    (is_sfence && (priv == PRIV_U || mstatus_tvm)));
    // Issue #33: with FS Off, an F instruction (FLW and FSW too) or any access to fflags, frm or fcsr
    // is illegal, in every mode; decided in DECODE, so it outranks the faults an FLW or FSW would take.
    // The emulator's floating_instruction(), the kernel's and tools/rv32_rtl.py's floating_word() match it.
    wire fs_illegal = mstatus_fs == 2'd0 &&
                      (fp_valid || fp_load || fp_store || (is_csr && csr_addr >= CSR_FFLAGS && csr_addr <= CSR_FCSR));

    // The page-table walk (issue #20). A read of the entry at pte_addr is a supervisor read: PMP
    // must grant it, it must lie in the 32-bit space, and only RAM answers it (mem_ptw keeps the
    // devices off it), else the access faults. The entry then decides, in the emulator's order:
    // invalid (V clear, or W without R), a pointer with D, A or U set or at level 0, a misaligned
    // megapage, U against the privilege (and SUM), the permission (and MXR), a clear A or, for a
    // store, D (Svade: hardware never sets them) are page faults; a leaf whose physical address
    // is at or past 2^32 is an access fault.
    wire walk_deny = (state == WALK) && (pte_addr[33:32] != 2'b00 || !pmp_ok);
    wire [21:0] pte_ppn = mem_rdata[31:10];
    wire pte_v = mem_rdata[0], pte_r = mem_rdata[1], pte_w = mem_rdata[2], pte_x = mem_rdata[3];
    wire pte_u = mem_rdata[4], pte_a = mem_rdata[6], pte_d = mem_rdata[7];
    wire pte_leaf = pte_r || pte_x;
    wire [1:0] walk_priv = walk_kind == WALK_FETCH ? priv : data_priv;
    wire [4:0] pte_bits = {pte_d, pte_u, pte_x, pte_w, pte_r};
    wire pte_page_fault = !pte_v || (!pte_r && pte_w) ||
                          (!pte_leaf && (!walk_level || pte_a || pte_d || pte_u)) ||
                          (pte_leaf && ((walk_level && pte_ppn[9:0] != 10'd0) || !pte_a ||
                                        leaf_denies(pte_bits, walk_kind, walk_priv, mstatus_sum, mstatus_mxr)));
    wire [33:0] leaf_addr = walk_level ? {pte_ppn[21:10], walk_va[21:0]} : {pte_ppn, walk_va[11:0]};
    wire [3:0] walk_page_cause = walk_kind == WALK_FETCH ? CAUSE_FETCH_PAGE :
                                 walk_kind == WALK_STORE ? CAUSE_STORE_PAGE : CAUSE_LOAD_PAGE;
    wire [3:0] walk_access_cause = walk_kind == WALK_FETCH ? CAUSE_FETCH_FAULT :
                                   walk_kind == WALK_STORE ? CAUSE_STORE_FAULT : CAUSE_LOAD_FAULT;
    wire [31:0] walk_epc = walk_kind == WALK_FETCH ? pc : ir_pc;
    wire [33:0] walk_root = {satp_ppn, 12'd0};
    // Cycles the walk adds, for the testbench's cycle formula: the FETCH cycle that starts one, each
    // WALK cycle whose request is not stalled (a stalled one counts as a stall, as any other), XLATE.
    // A TLB hit adds none.
    wire ptw_cycle = (fetch_walk && !irq_take) || (state == WALK && !(mem_valid && !mem_ready)) || state == XLATE;
    // TLB lookups for the testbench (issue #24), one pulse each: a miss on the cycle that starts its
    // walk, a fetch's hit on the edge that leaves FETCH, a load's or store's hit in EXECUTE.
    wire tlb_miss_seen = (fetch_walk && !irq_take) || (data_translating && !tlb_hit);
    wire tlb_hit_seen = (fetch_translating && tlb_hit && !irq_take && (fetch_page_fault || fetch_deny || mem_ready)) ||
                        (data_translating && tlb_hit);
    // The testbench reads ptw_cycle and the TLB pulses; PMP and the reservation compare whole words.
    wire unused_ok = &{1'b0, ptw_cycle, tlb_miss_seen, tlb_hit_seen, pmp_addr[1:0], ram_snoop_addr[1:0]};

    assign mem_valid = !reset && ((state == FETCH && !irq_take && !fetch_deny && !fetch_walk && !fetch_page_fault) ||
                                  (state == MEM) || (state == AMO_WRITE) ||
                                  (state == WALK && !walk_deny));
    assign mem_fetch = mem_valid && (state == FETCH);
    assign mem_ptw = mem_valid && (state == WALK);
    assign mem_addr = mem_fetch ? access_addr : mem_ptw ? pte_addr[31:0] : xlate_ok ? phys : alu_out;
    // An AMO reads in MEM and writes in AMO_WRITE; any other access writes in MEM if it writes.
    assign mem_we = mem_valid && ((state == MEM && mem_writes && !mem_reads) || state == AMO_WRITE);
    assign mem_strb = !mem_valid ? 4'b0000 : (mem_fetch || mem_ptw) ? 4'b1111 : strb;
    // Sub-word store data is replicated across the lanes so the strobe alone selects it.
    assign mem_wdata = (state == AMO_WRITE) ? amo_value : (width == 2'd0) ? {4{b[7:0]}} : (width == 2'd1) ? {2{b[15:0]}} : b;

    // Trap entry: report it on the retirement port; vector through mtvec
    // unless the previous trap's handler has not retired yet, which halts
    // the core with the CSRs of the first trap intact. Since issue #20 a trap
    // from S or U mode whose cause medeleg (an exception) or mideleg (an
    // interrupt) delegates enters S mode through stvec and the supervisor's CSRs.
    task take_trap;
        input interrupt;
        input [3:0] cause;
        input [31:0] value;
        input [31:0] epc;
        begin
            trap <= 1'b1;
            trap_interrupt <= interrupt;
            trap_cause <= cause;
            trap_value <= value;
            xlate_ok <= 1'b0;
            reserved <= 1'b0; // issue #34: a trap ends the reservation
            if (in_trap) begin
                state <= HALT;
                halted <= 1'b1;
            end else if (!priv_m && (interrupt ? mideleg_value[{1'b0, cause}] : medeleg[cause])) begin
                in_trap <= 1'b1;
                sepc <= epc;
                scause <= {interrupt, 27'd0, cause};
                stval <= value;
                mstatus_spie <= mstatus_sie;
                mstatus_sie <= 1'b0;
                mstatus_spp <= priv == PRIV_S;
                priv <= PRIV_S;
                pc <= stvec;
                state <= FETCH;
            end else begin
                in_trap <= 1'b1;
                mepc <= epc;
                mcause <= {interrupt, 27'd0, cause};
                mstatus_mpie <= mstatus_mie;
                mstatus_mie <= 1'b0;
                mpp <= priv;
                priv <= PRIV_M;
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
            fcsr <= 8'd0; fa <= 32'd0; fb <= 32'd0; fc <= 32'd0; fp_flags <= 5'd0;
            retire_fd_we <= 1'b0; retire_fd <= 5'd0; retire_fd_value <= 32'd0;
            retire_fcsr_we <= 1'b0; retire_fcsr <= 8'd0;
            in_trap <= 1'b0;
            mstatus_mie <= 1'b0;
            mstatus_mpie <= 1'b0;
            mie_bits <= 3'd0;
            priv <= PRIV_M;
            mpp <= PRIV_M;
            mcounteren <= 3'd0;
            mstatus_sie <= 1'b0; mstatus_spie <= 1'b0; mstatus_spp <= 1'b0; mstatus_mprv <= 1'b0;
            mstatus_sum <= 1'b0; mstatus_mxr <= 1'b0; mstatus_tvm <= 1'b0; mstatus_tw <= 1'b0; mstatus_tsr <= 1'b0;
            mstatus_fs <= 2'd3;
            medeleg <= 16'd0; mideleg <= 3'd0; mie_s <= 3'd0; mip_soft <= 3'd0; scounteren <= 3'd0;
            stvec <= 32'd0; sscratch <= 32'd0; sepc <= 32'd0; scause <= 32'd0; stval <= 32'd0;
            satp_mode <= 1'b0; satp_ppn <= 22'd0;
            xlate_ok <= 1'b0; walk_level <= 1'b0; walk_kind <= WALK_FETCH; phys <= 32'd0; pte_addr <= 34'd0;
            tlb_valid <= 4'd0; tlb_next <= 2'd0; // the entries themselves need no reset
            reserved <= 1'b0; sc_held <= 1'b0; reservation <= 30'd0; reservation_pa <= 30'd0;
            for (entry = 0; entry < 8; entry = entry + 1) begin
                pmpcfg[entry] <= 8'd0;
                pmpaddr[entry] <= 32'd0;
            end
            mscratch <= 32'd0;
            fetch_waiting <= 1'b0;
            trap_interrupt <= 1'b0;
            cycle_count <= 64'd0;
            instret_count <= 64'd0;
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
            // In step-tick mode a tick is the pulse of the step completed in the previous cycle.
            cycle_count <= cycle_count + (step_ticks ? {63'd0, retire || trap} : 64'd1);
            fetch_waiting <= (state == FETCH) && mem_valid && !mem_ready;
            // Issue #34: a device wrote LR.W's reserved word. It cannot coincide with this core's
            // own access (the RAM has one port), so no later assignment this cycle competes.
            if (ram_snoop_write && ram_snoop_addr[31:2] == reservation_pa) reserved <= 1'b0;
            case (state)
                FETCH: if (irq_take) begin
                    retire_pc <= pc;
                    retire_insn <= 32'd0;
                    take_trap(1'b1, irq_code, 32'd0, pc);
                end else if (fetch_walk) begin // Sv32: translate the pc first (issue #20)
                    walk_kind <= WALK_FETCH;
                    walk_level <= 1'b1;
                    pte_addr <= walk_root + {22'd0, pc[31:22], 2'b00};
                    state <= WALK;
                end else if (fetch_page_fault || fetch_deny) begin
                    // A TLB hit the fetch may not use (issue #24) goes before PMP, which refuses the
                    // fetch before the bus sees it (O5).
                    retire_pc <= pc;
                    retire_insn <= 32'd0;
                    take_trap(1'b0, fetch_page_fault ? CAUSE_FETCH_PAGE : CAUSE_FETCH_FAULT, pc, pc);
                end else if (mem_ready) begin
                    xlate_ok <= 1'b0;
                    ir <= mem_rdata;
                    ir_pc <= pc;
                    retire_pc <= pc;
                    retire_insn <= mem_error ? 32'd0 : mem_rdata;
                    if (mem_error)
                        take_trap(1'b0, CAUSE_FETCH_FAULT, pc, pc);
                    else
                        state <= DECODE;
                end
                DECODE: begin
                    a <= rs1_value;
                    b <= fp_store ? f2 : rs2_value;
                    fa <= fp_from_integer ? rs1_value : f1;
                    fb <= f2; fc <= f3; fp_flags <= 5'd0;
                    if ((illegal && !fp_valid) || priv_illegal || fs_illegal)
                        take_trap(1'b0, CAUSE_ILLEGAL, ir, ir_pc);
                    else if (is_ecall)
                        take_trap(1'b0, priv_m ? CAUSE_ECALL : priv == PRIV_S ? CAUSE_ECALL_S : CAUSE_ECALL_U, 32'd0, ir_pc);
                    else if (is_ebreak)
                        take_trap(1'b0, CAUSE_BREAKPOINT, ir_pc, ir_pc);
                    else
                        state <= EXECUTE;
                end
                EXECUTE: begin
                    alu_out <= execute_out;
                    taken <= branch_taken;
                    sc_held <= !sc_skip;
                    if (access_misaligned)
                        take_trap(1'b0, is_load ? CAUSE_LOAD_MISALIGNED : CAUSE_STORE_MISALIGNED, alu_result, ir_pc);
                    else if (sc_skip) // issue #34: a failing SC.W retires with no access
                        state <= WRITEBACK;
                    else if (data_translating && !tlb_hit) begin // Sv32: a TLB miss walks first (issue #20, #24)
                        walk_kind <= is_store ? WALK_STORE : WALK_LOAD;
                        walk_level <= 1'b1;
                        pte_addr <= walk_root + {22'd0, alu_result[31:22], 2'b00};
                        state <= WALK;
                    end
                    else if (data_page_fault)
                        take_trap(1'b0, is_load ? CAUSE_LOAD_PAGE : CAUSE_STORE_PAGE, alu_result, ir_pc);
                    else if ((is_load || is_store) && !pmp_ok) // PMP, before the bus (O5); a hit's physical address
                        take_trap(1'b0, is_load ? CAUSE_LOAD_FAULT : CAUSE_STORE_FAULT, alu_result, ir_pc);
                    else if (target_misaligned)
                        take_trap(1'b0, CAUSE_TARGET_MISALIGNED, execute_out, ir_pc);
                    else if (fp_valid) begin
                        if (fp_direct != DIRECT_NONE) begin
                            alu_out <= direct_result;
                            state <= WRITEBACK;
                        end else state <= FP_ISSUE;
                    end
                    else if (md_start)
                        state <= MD_WAIT;
                    else if (is_wfi && !step_ticks && !irq_wake)
                        state <= WFI_WAIT;
                    else if (is_load || is_store) begin
                        phys <= tlb_phys;          // used only when translated: a TLB hit
                        xlate_ok <= translate_data;
                        state <= MEM;
                    end else
                        state <= WRITEBACK;
                end
                FP_ISSUE: if (fp_ready) state <= FP_WAIT;
                FP_WAIT: if (fp_done) begin
                    // Defensive guard: legal decode never issues an invalid op/rm.
                    if (fp_error) take_trap(1'b0, CAUSE_ILLEGAL, ir, ir_pc);
                    else begin
                        alu_out <= fp_result;
                        fp_flags <= fp_result_flags;
                        state <= WRITEBACK;
                    end
                end
                // wfi waits for any enabled interrupt level, whatever mstatus.MIE says.
                WFI_WAIT: if (irq_wake) state <= WRITEBACK;
                // Sv32 (issue #20): one page-table entry per visit, then FETCH with the translated
                // pc, XLATE for a data address, or a trap. The trap reports the virtual address.
                WALK: if (walk_deny) begin
                    if (walk_kind == WALK_FETCH) begin retire_pc <= pc; retire_insn <= 32'd0; end
                    take_trap(1'b0, walk_access_cause, walk_va, walk_epc);
                end else if (mem_ready) begin
                    if (walk_kind == WALK_FETCH && (mem_error || pte_page_fault)) begin
                        retire_pc <= pc;
                        retire_insn <= 32'd0;
                    end
                    if (mem_error)
                        take_trap(1'b0, walk_access_cause, walk_va, walk_epc);
                    else if (pte_page_fault)
                        take_trap(1'b0, walk_page_cause, walk_va, walk_epc);
                    else if (!pte_leaf) begin
                        walk_level <= 1'b0;
                        pte_addr <= {pte_ppn, 12'd0} + {22'd0, walk_va[21:12], 2'b00};
                    end else if (leaf_addr[33:32] != 2'b00) begin
                        if (walk_kind == WALK_FETCH) begin retire_pc <= pc; retire_insn <= 32'd0; end
                        take_trap(1'b0, walk_access_cause, walk_va, walk_epc);
                    end else begin
                        phys <= leaf_addr[31:0];
                        xlate_ok <= 1'b1;
                        state <= walk_kind == WALK_FETCH ? FETCH : XLATE;
                        // Fill the TLB (issue #24) before PMP: XLATE or the fetch still checks the address.
                        tlb_valid[tlb_next] <= 1'b1;
                        tlb_mega[tlb_next] <= walk_level;
                        tlb_vpn[tlb_next] <= walk_va[31:12];
                        tlb_ppn[tlb_next] <= pte_ppn[19:0];
                        tlb_perm[tlb_next] <= pte_bits;
                        tlb_next <= tlb_next + 2'd1;
                    end
                end
                // PMP on the translated data address, at the effective privilege.
                XLATE: if (!pmp_ok)
                    take_trap(1'b0, is_load ? CAUSE_LOAD_FAULT : CAUSE_STORE_FAULT, alu_out, ir_pc);
                else
                    state <= MEM;
                MD_WAIT: if (md_valid) begin
                    alu_out <= md_result;
                    state <= WRITEBACK;
                end
                MEM: if (mem_ready) begin
                    mdr <= mem_rdata;
                    if (mem_error)
                        take_trap(1'b0, is_load ? CAUSE_LOAD_FAULT : CAUSE_STORE_FAULT, alu_out, ir_pc);
                    else begin
                        state <= is_amo ? AMO_WRITE : WRITEBACK;
                        // Issue #34: LR.W reserves its word as the read is accepted, so a device's
                        // write from the next cycle on ends the reservation.
                        if (is_lr) begin
                            reserved <= 1'b1;
                            reservation <= alu_out[31:2];
                            reservation_pa <= mem_addr[31:2];
                        end
                    end
                end
                // Issue #34: the AMO writes its result to the word MEM read, at the same address
                // (when translated, `phys` and xlate_ok still hold the read's translation). A refused
                // write is an access fault.
                AMO_WRITE: if (mem_ready) begin
                    if (mem_error)
                        take_trap(1'b0, CAUSE_STORE_FAULT, alu_out, ir_pc);
                    else
                        state <= WRITEBACK;
                end
                WRITEBACK: begin
                    // The register file samples rf_we/rd_value on this same edge;
                    // a CSR write lands here too, so the instruction's effects commit together.
                    pc <= is_mret ? mepc : is_sret ? sepc : (is_jal || is_jalr || taken) ? alu_out : ir_pc + 32'd4;
                    xlate_ok <= 1'b0;
                    retire_fd_we <= fp_write;
                    retire_fd <= rd;
                    retire_fd_value <= fp_value;
                    retire_fcsr_we <= fp_csr_write || (fp_valid && fp_flags != 5'd0);
                    retire_fcsr <= fcsr_next;
                    if (fp_csr_write || fp_valid) fcsr <= fcsr_next;
                    // Issue #33: an f register written or fcsr changed (the retire port's fd and fcsr
                    // effects) makes the state Dirty. No such instruction writes mstatus as well.
                    if (fp_write || fp_csr_write || (fp_valid && fp_flags != 5'd0)) mstatus_fs <= 2'd3;
                    if (csr_we) begin
                        case (csr_addr)
                            CSR_MTVEC: mtvec <= {csr_new[31:2], 2'b00}; // direct mode only
                            CSR_MEPC: mepc <= {csr_new[31:2], 2'b00};   // IALIGN is 32
                            CSR_MCAUSE: mcause <= csr_new;
                            CSR_MTVAL: mtval <= csr_new;
                            CSR_MSTATUS: begin
                                mstatus_sie <= csr_new[1];
                                mstatus_mie <= csr_new[3];
                                mstatus_spie <= csr_new[5];
                                mstatus_mpie <= csr_new[7];
                                mstatus_spp <= csr_new[8];
                                mpp <= csr_new[12:11] == 2'b10 ? PRIV_U : csr_new[12:11]; // WARL: 3, 1 or 0
                                mstatus_mprv <= csr_new[17];
                                mstatus_sum <= csr_new[18];
                                mstatus_mxr <= csr_new[19];
                                mstatus_tvm <= csr_new[20];
                                mstatus_tw <= csr_new[21];
                                mstatus_tsr <= csr_new[22];
                                mstatus_fs <= csr_new[14:13];
                            end
                            CSR_SSTATUS: begin
                                mstatus_sie <= csr_new[1];
                                mstatus_spie <= csr_new[5];
                                mstatus_spp <= csr_new[8];
                                mstatus_fs <= csr_new[14:13];
                                mstatus_sum <= csr_new[18];
                                mstatus_mxr <= csr_new[19];
                            end
                            CSR_MEDELEG: medeleg <= csr_new[15:0] & 16'hb3ff; // not 10, 11 or 14
                            CSR_MIDELEG: mideleg <= csr_new_s;
                            CSR_SIE: mie_s <= (mie_s & ~mideleg) | (csr_new_s & mideleg);
                            CSR_SIP: if (mideleg[0]) mip_soft[0] <= csr_new[1]; // only SSIP, when delegated
                            CSR_STVEC: stvec <= {csr_new[31:2], 2'b00};
                            CSR_SCOUNTEREN: scounteren <= csr_new[2:0];
                            CSR_SSCRATCH: sscratch <= csr_new;
                            CSR_SEPC: sepc <= {csr_new[31:2], 2'b00};
                            CSR_SCAUSE: scause <= csr_new;
                            CSR_STVAL: stval <= csr_new;
                            CSR_SATP: begin
                                satp_mode <= csr_new[31];
                                satp_ppn <= csr_new[21:0];
                            end
                            CSR_MCOUNTEREN: mcounteren <= csr_new[2:0];
                            CSR_PMPCFG0, CSR_PMPCFG1:
                                for (entry = 0; entry < 4; entry = entry + 1)
                                    if (!pmpcfg[{csr_addr[0], entry[1:0]}][7]) // a locked entry ignores writes
                                        // Bits 6:5 read 0; W without R is reserved and stored as neither.
                                        pmpcfg[{csr_addr[0], entry[1:0]}] <=
                                            (csr_new[8 * entry +: 8] & 8'h9f) & ~{6'd0, csr_new[8 * entry + 1] && !csr_new[8 * entry], 1'b0};
                            CSR_MIE: begin
                                mie_bits <= {csr_new[11], csr_new[7], csr_new[3]};
                                mie_s <= csr_new_s;
                            end
                            CSR_MSCRATCH: mscratch <= csr_new;
                            // mip: MSIP, MTIP and MEIP are the devices'; SSIP, STIP and SEIP are software's.
                            CSR_MIP: mip_soft <= csr_new_s;
                            default: // pmpaddr0-7, unless the entry or the TOR entry above it is locked
                                if (csr_addr[11:3] == 9'h076 && !pmpcfg[csr_addr[2:0]][7] &&
                                    !(csr_addr[2:0] != 3'd7 && pmpcfg[csr_addr[2:0] + 3'd1][7] &&
                                      pmpcfg[csr_addr[2:0] + 3'd1][4:3] == 2'd1))
                                    pmpaddr[csr_addr[2:0]] <= csr_new;
                        endcase
                    end
                    if (is_mret) begin
                        mstatus_mie <= mstatus_mpie;
                        mstatus_mpie <= 1'b1;
                        priv <= mpp;      // O5: the mode MPP names,
                        mpp <= PRIV_U;    // and MPP becomes the least privileged mode;
                        if (mpp != PRIV_M) mstatus_mprv <= 1'b0; // MPRV lasts only while machine mode does
                    end
                    if (is_sret) begin    // issue #20: the same from S mode's copies
                        mstatus_sie <= mstatus_spie;
                        mstatus_spie <= 1'b1;
                        priv <= mstatus_spp ? PRIV_S : PRIV_U;
                        mstatus_spp <= 1'b0;
                        mstatus_mprv <= 1'b0;
                    end
                    // Issue #34: SC.W, mret and sret end any reservation (QEMU clears it on xRET too,
                    // which the privileged specification allows), and so does a new translation: a
                    // satp write or sfence.vma, after which the virtual word may name another page.
                    if (is_sc || is_mret || is_sret || is_sfence || (csr_we && csr_addr == CSR_SATP))
                        reserved <= 1'b0;
                    if (is_sfence || (csr_we && csr_addr == CSR_SATP)) begin // issue #24: flush every entry
                        tlb_valid <= 4'd0;
                        tlb_next <= 2'd0;
                    end
                    in_trap <= 1'b0;
                    instret_count <= instret_count + 64'd1;
                    retire <= 1'b1;
                    retire_rd_we <= rd_written;
                    retire_rd <= rd;
                    retire_rd_value <= rd_value;
                    state <= FETCH;
                end
                HALT: begin end // hold until reset
                // Encodings 13-15 are never entered; should the state register
                // ever hold one, stop the way a double fault does rather than
                // hang with `halted` low.
                default: begin
                    state <= HALT;
                    halted <= 1'b1;
                end
            endcase
        end
    end
endmodule
