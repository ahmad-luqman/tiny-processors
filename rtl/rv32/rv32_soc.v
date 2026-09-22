`timescale 1ns/1ps

// The RV32 machine as one synthesizable unit: the core, the bus decoder, and
// every memory and device of docs/rv32.md. The core's memory port and its
// retirement port are passed through unchanged so the testbench can check
// the handshake and print the trace exactly as it did when it was the
// memory itself; `mem_hold` lets it defer every acceptance to model a slow
// memory. Device side effects that need a host (console bytes, the done
// word, a present) leave as strobes and the host's key events come in
// through the input push port; the host decides what they mean.
module rv32_soc #(
    parameter integer RAM_WORDS = 1048576,
    parameter integer FB_WORDS = 19200, // 320 x 240 one-byte pixels, as 32-bit words
    parameter integer CONSOLE_BUSY = 0
) (
    input  wire        clk,
    input  wire        reset,
    input  wire        mem_hold,
    input  wire        simd_memory_hold,
    // Core memory port, observed.
    output wire        mem_valid,
    output wire [31:0] mem_addr,
    output wire        mem_we,
    output wire [3:0]  mem_strb,
    output wire [31:0] mem_wdata,
    output wire        mem_ready,
    output wire [31:0] mem_rdata,
    output wire        mem_error,
    output wire        mem_fetch,
    // Retirement port, from the core.
    output wire        retire,
    output wire [31:0] retire_pc,
    output wire [31:0] retire_insn,
    output wire        retire_rd_we,
    output wire [4:0]  retire_rd,
    output wire [31:0] retire_rd_value,
    output wire        retire_fd_we,
    output wire [4:0]  retire_fd,
    output wire [31:0] retire_fd_value,
    output wire        retire_fcsr_we,
    output wire [7:0]  retire_fcsr,
    output wire        trap,
    output wire [3:0]  trap_cause,
    output wire [31:0] trap_value,
    output wire        halted,
    output wire [2:0]  state,
    output wire [31:0] pc,
    output wire [31:0] mtvec,
    output wire [31:0] mepc,
    output wire [31:0] mcause,
    output wire [31:0] mtval,
    // Device side effects for the host.
    output wire        console_valid,
    output wire [7:0]  console_byte,
    output wire        done_valid,
    output wire [31:0] done_wdata,
    output wire        display_present,
    output wire [31:0] display_frames,
    input  wire        in_push,
    input  wire [31:0] in_event,
    output wire        in_full
);
    wire simd_valid, simd_ready, simd_error;
    wire [31:0] simd_rdata;
    wire ram_valid, ram_ready, ram_error;
    wire [31:0] ram_rdata;
    wire con_valid, con_ready, con_error;
    wire [31:0] con_rdata;
    wire dn_valid, dn_ready, dn_error;
    wire [31:0] dn_rdata;
    wire tm_valid, tm_ready, tm_error;
    wire [31:0] tm_rdata;
    wire in_valid, in_ready, in_error;
    wire [31:0] in_rdata;
    wire dp_valid, dp_ready, dp_error;
    wire [31:0] dp_rdata;
    wire fb_valid, fb_ready, fb_error;
    wire [31:0] fb_rdata;

    rv32 core (
        .clk(clk), .reset(reset),
        .mem_valid(mem_valid), .mem_addr(mem_addr), .mem_we(mem_we), .mem_strb(mem_strb),
        .mem_wdata(mem_wdata), .mem_ready(mem_ready), .mem_rdata(mem_rdata), .mem_error(mem_error),
        .mem_fetch(mem_fetch),
        .retire(retire), .retire_pc(retire_pc), .retire_insn(retire_insn), .retire_rd_we(retire_rd_we),
        .retire_rd(retire_rd), .retire_rd_value(retire_rd_value), .trap(trap), .trap_cause(trap_cause),
        .retire_fd_we(retire_fd_we), .retire_fd(retire_fd), .retire_fd_value(retire_fd_value),
        .retire_fcsr_we(retire_fcsr_we), .retire_fcsr(retire_fcsr),
        .trap_value(trap_value), .halted(halted), .state(state), .pc(pc),
        .mtvec(mtvec), .mepc(mepc), .mcause(mcause), .mtval(mtval)
    );

    rv32_bus #(.RAM_WORDS(RAM_WORDS), .FB_WORDS(FB_WORDS)) bus (
        .mem_valid(mem_valid), .mem_we(mem_we), .mem_fetch(mem_fetch), .mem_hold(mem_hold),
        .mem_addr(mem_addr), .mem_ready(mem_ready), .mem_error(mem_error), .mem_rdata(mem_rdata),
        .ram_valid(ram_valid), .ram_ready(ram_ready), .ram_error(ram_error), .ram_rdata(ram_rdata),
        .console_valid(con_valid), .console_ready(con_ready), .console_error(con_error), .console_rdata(con_rdata),
        .done_valid(dn_valid), .done_ready(dn_ready), .done_error(dn_error), .done_rdata(dn_rdata),
        .timer_valid(tm_valid), .timer_ready(tm_ready), .timer_error(tm_error), .timer_rdata(tm_rdata),
        .input_valid(in_valid), .input_ready(in_ready), .input_error(in_error), .input_rdata(in_rdata),
        .display_valid(dp_valid), .display_ready(dp_ready), .display_error(dp_error), .display_rdata(dp_rdata),
        .fb_valid(fb_valid), .fb_ready(fb_ready), .fb_error(fb_error), .fb_rdata(fb_rdata),
        .simd_valid(simd_valid), .simd_ready(simd_ready), .simd_error(simd_error), .simd_rdata(simd_rdata)
    );

    rv32_simd4 accelerator (
        .clk(clk), .reset(reset), .valid(simd_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(simd_rdata), .ready(simd_ready), .error(simd_error),
        .memory_hold(simd_memory_hold)
    );

    // The memories index from their window's base, which is the bus's business: the two
    // BASE values below repeat rv32_bus.v's RAM_BASE and FB_BASE, and a test pins them equal.
    rv32_ram #(.WORDS(RAM_WORDS), .BASE(32'h8000_0000)) ram (
        .clk(clk), .reset(reset), .valid(ram_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(ram_rdata), .ready(ram_ready), .error(ram_error)
    );

    rv32_console #(.BUSY_CYCLES(CONSOLE_BUSY)) console (
        .clk(clk), .reset(reset), .valid(con_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(con_rdata), .ready(con_ready), .error(con_error),
        .tx_valid(console_valid), .tx_byte(console_byte)
    );

    rv32_done done (
        .clk(clk), .reset(reset), .valid(dn_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(dn_rdata), .ready(dn_ready), .error(dn_error),
        .done_valid(done_valid), .done_word(done_wdata)
    );

    rv32_timer timer (
        .clk(clk), .reset(reset), .valid(tm_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(tm_rdata), .ready(tm_ready), .error(tm_error)
    );

    rv32_input input_device (
        .clk(clk), .reset(reset), .valid(in_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(in_rdata), .ready(in_ready), .error(in_error),
        .push(in_push), .push_event(in_event), .full(in_full)
    );

    rv32_display display (
        .clk(clk), .reset(reset), .valid(dp_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(dp_rdata), .ready(dp_ready), .error(dp_error),
        .present(display_present), .frames(display_frames)
    );

    // The pixels: ordinary memory behind its own window (docs/rv32.md, "framebuffer").
    rv32_ram #(.WORDS(FB_WORDS), .BASE(32'h3000_0000)) fb (
        .clk(clk), .reset(reset), .valid(fb_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(fb_rdata), .ready(fb_ready), .error(fb_error)
    );
endmodule
