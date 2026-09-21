`timescale 1ns/1ps

// Bus decoder (docs/rv32.md, "Memory map"): turns the core's address into
// one peripheral select, hands the request to that slave, and returns its
// answer. Everything is combinational, so a slave that answers in the same
// cycle costs the core nothing beyond the cycle the request is presented.
//
// Three rules compose here. A request reaches a slave only as
// `<slave>_valid = req & <slave>_sel`, where `req = mem_valid & ~mem_hold`:
// while the host holds the bus, no slave sees the request and `mem_ready`
// stays low, which is how the testbench models a slow memory. Only RAM is
// fetchable: every device select is gated with `~mem_fetch`, so a jump into
// a device window is refused like an unmapped address. An address that
// selects nothing is answered at once with `ready` and `error`.
//
// Adding a slave means one select, one `_valid`, one term in `none_sel`, and
// one term in each of the three OR-reductions below. Forgetting `none_sel`
// makes the new window answer as unmapped (`error`) and as its slave at once.
module rv32_bus #(
    parameter integer RAM_WORDS = 1048576,
    parameter integer FB_WORDS = 19200
) (
    // Core side.
    input  wire        mem_valid,
    input  wire        mem_we,
    input  wire        mem_fetch,
    input  wire        mem_hold,
    input  wire [31:0] mem_addr,
    output wire        mem_ready,
    output wire        mem_error,
    output wire [31:0] mem_rdata,
    // RAM at 0x8000_0000.
    output wire        ram_valid,
    input  wire        ram_ready,
    input  wire        ram_error,
    input  wire [31:0] ram_rdata,
    // Console at 0x1000_0000.
    output wire        console_valid,
    input  wire        console_ready,
    input  wire        console_error,
    input  wire [31:0] console_rdata,
    // Done register at 0x0010_0000.
    output wire        done_valid,
    input  wire        done_ready,
    input  wire        done_error,
    input  wire [31:0] done_rdata,
    // Timer at 0x2000_0000.
    output wire        timer_valid,
    input  wire        timer_ready,
    input  wire        timer_error,
    input  wire [31:0] timer_rdata,
    // Input at 0x2000_1000.
    output wire        input_valid,
    input  wire        input_ready,
    input  wire        input_error,
    input  wire [31:0] input_rdata,
    // Display controller at 0x2000_2000.
    output wire        display_valid,
    input  wire        display_ready,
    input  wire        display_error,
    input  wire [31:0] display_rdata,
    // Framebuffer at 0x3000_0000.
    output wire        fb_valid,
    input  wire        fb_ready,
    input  wire        fb_error,
    input  wire [31:0] fb_rdata
);
    localparam [31:0] RAM_BASE = 32'h8000_0000;
    localparam [31:0] RAM_BYTES = RAM_WORDS * 4;
    localparam [31:0] CONSOLE_BASE = 32'h1000_0000;
    localparam [31:0] DONE_ADDR = 32'h0010_0000;
    localparam [31:0] TIMER_BASE = 32'h2000_0000;
    localparam [31:0] INPUT_BASE = 32'h2000_1000;
    localparam [31:0] DISPLAY_BASE = 32'h2000_2000;
    localparam [31:0] FB_BASE = 32'h3000_0000;
    localparam [31:0] FB_BYTES = FB_WORDS * 4;

    wire req = mem_valid && !mem_hold;
    wire [31:0] ram_offset = mem_addr - RAM_BASE;
    wire [31:0] fb_offset = mem_addr - FB_BASE;

    // One comparator per window; the windows are disjoint so at most one is set. A memory
    // window tests only the access's first byte: that equals the emulator's whole-access test
    // because accesses are naturally aligned (misalignment traps before decode), every window
    // base is word aligned, and every window size is a multiple of four; keep it so for any
    // window added later.
    wire ram_sel = (mem_addr >= RAM_BASE) && (ram_offset < RAM_BYTES);
    wire console_sel = !mem_fetch && (mem_addr[31:3] == CONSOLE_BASE[31:3]);
    wire done_sel = !mem_fetch && (mem_addr == DONE_ADDR);
    wire timer_sel = !mem_fetch && (mem_addr[31:4] == TIMER_BASE[31:4]);
    wire input_sel = !mem_fetch && (mem_addr[31:4] == INPUT_BASE[31:4]);
    wire display_sel = !mem_fetch && (mem_addr[31:4] == DISPLAY_BASE[31:4]);
    wire fb_sel = !mem_fetch && (mem_addr >= FB_BASE) && (fb_offset < FB_BYTES);
    wire none_sel = !(ram_sel || console_sel || done_sel || timer_sel || input_sel || display_sel || fb_sel);

    assign ram_valid = req && ram_sel;
    assign console_valid = req && console_sel;
    assign done_valid = req && done_sel;
    assign timer_valid = req && timer_sel;
    assign input_valid = req && input_sel;
    assign display_valid = req && display_sel;
    assign fb_valid = req && fb_sel;

    assign mem_ready = req && ((ram_sel && ram_ready) || (console_sel && console_ready) ||
                               (done_sel && done_ready) || (timer_sel && timer_ready) ||
                               (input_sel && input_ready) || (display_sel && display_ready) ||
                               (fb_sel && fb_ready) || none_sel);
    assign mem_error = (ram_sel && ram_error) || (console_sel && console_error) ||
                       (done_sel && done_error) || (timer_sel && timer_error) ||
                       (input_sel && input_error) || (display_sel && display_error) ||
                       (fb_sel && fb_error) || none_sel;
    assign mem_rdata = ({32{ram_sel}} & ram_rdata) | ({32{console_sel}} & console_rdata) |
                       ({32{done_sel}} & done_rdata) | ({32{timer_sel}} & timer_rdata) |
                       ({32{input_sel}} & input_rdata) | ({32{display_sel}} & display_rdata) |
                       ({32{fb_sel}} & fb_rdata);

    // `mem_we` is routed to the slaves by the machine, not decoded here: a write to a
    // read-only register is the slave's refusal, so the decoder stays direction-blind.
    wire unused_ok = &{1'b0, mem_we};
endmodule
