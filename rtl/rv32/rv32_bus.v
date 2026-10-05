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
    parameter integer RAM_WORDS = 4194304,
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
    // CLINT at 0x0200_0000 (64 KiB, QEMU virt's address).
    output wire        clint_valid,
    input  wire        clint_ready,
    input  wire        clint_error,
    input  wire [31:0] clint_rdata,
    // PLIC at 0x0c00_0000 (6 MiB, QEMU virt's address; O1).
    output wire        plic_valid,
    input  wire        plic_ready,
    input  wire        plic_error,
    input  wire [31:0] plic_rdata,
    // virtio-blk at 0x1000_1000 (512 bytes of virt's first 4 KiB virtio slot; O3).
    output wire        virtio_valid,
    input  wire        virtio_ready,
    input  wire        virtio_error,
    input  wire [31:0] virtio_rdata,
    // Boot ROM (the device tree) at 0x0000_1000, 4 KiB.
    output wire        rom_valid,
    input  wire        rom_ready,
    input  wire        rom_error,
    input  wire [31:0] rom_rdata,
    // Input at 0x1100_1000.
    output wire        input_valid,
    input  wire        input_ready,
    input  wire        input_error,
    input  wire [31:0] input_rdata,
    // Display controller at 0x1100_2000.
    output wire        display_valid,
    input  wire        display_ready,
    input  wire        display_error,
    input  wire [31:0] display_rdata,
    // Palette at 0x1100_3000, 1 KiB (issue #35).
    output wire        palette_valid,
    input  wire        palette_ready,
    input  wire        palette_error,
    input  wire [31:0] palette_rdata,
    // Framebuffer at 0x1200_0000.
    output wire        fb_valid,
    input  wire        fb_ready,
    input  wire        fb_error,
    input  wire [31:0] fb_rdata,
    output wire gpu_valid,
    input wire gpu_ready, gpu_error,
    input wire [31:0] gpu_rdata,
    output wire simd_valid,
    input wire simd_ready, simd_error,
    input wire [31:0] simd_rdata,
    output wire g3d_valid,
    input wire g3d_ready, g3d_error,
    input wire [31:0] g3d_rdata,
    output wire dma_window_valid,
    input wire dma_window_ready, dma_window_error,
    input wire [31:0] dma_window_rdata
);
    localparam [31:0] GPU_BASE = 32'h1100_7000;
    localparam [31:0] G3D_BASE = 32'h1100_8000;
    localparam [31:0] DMA_WINDOW_BASE = 32'h1100_a000;
    localparam [31:0] SIMD4_BASE = 32'h1100_4000;
    localparam [31:0] SIMD4_PROGRAM = 32'h1100_5000;
    localparam [31:0] SIMD4_DATA = 32'h1100_6000;
    localparam [31:0] RAM_BASE = 32'h8000_0000;
    localparam [31:0] RAM_BYTES = RAM_WORDS * 4;
    localparam [31:0] CONSOLE_BASE = 32'h1000_0000;
    localparam [31:0] DONE_ADDR = 32'h0010_0000;
    localparam [31:0] CLINT_BASE = 32'h0200_0000;
    localparam [31:0] PLIC_BASE = 32'h0c00_0000;
    localparam [31:0] VIRTIO_BASE = 32'h1000_1000;
    localparam [31:0] BOOTROM_BASE = 32'h0000_1000;
    localparam [31:0] INPUT_BASE = 32'h1100_1000;
    localparam [31:0] DISPLAY_BASE = 32'h1100_2000;
    localparam [31:0] PALETTE_BASE = 32'h1100_3000;
    localparam [31:0] FB_BASE = 32'h1200_0000;
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
    wire clint_sel = !mem_fetch && (mem_addr[31:16] == CLINT_BASE[31:16]);
    // The PLIC's window is 6 MiB: the first three 2 MiB blocks above its base.
    wire plic_sel = !mem_fetch && (mem_addr[31:23] == PLIC_BASE[31:23]) && (mem_addr[22:21] != 2'b11);
    wire virtio_sel = !mem_fetch && (mem_addr[31:9] == VIRTIO_BASE[31:9]);
    wire rom_sel = !mem_fetch && (mem_addr[31:12] == BOOTROM_BASE[31:12]);
    wire input_sel = !mem_fetch && (mem_addr[31:4] == INPUT_BASE[31:4]);
    wire display_sel = !mem_fetch && (mem_addr[31:4] == DISPLAY_BASE[31:4]);
    wire palette_sel = !mem_fetch && (mem_addr[31:10] == PALETTE_BASE[31:10]);
    wire fb_sel = !mem_fetch && (mem_addr >= FB_BASE) && (fb_offset < FB_BYTES);
    wire simd_sel = !mem_fetch && (((mem_addr & 32'hffff_ffe0) == SIMD4_BASE) ||
                      ((mem_addr & 32'hffff_fc00) == SIMD4_PROGRAM) ||
                      ((mem_addr & 32'hffff_fc00) == SIMD4_DATA));
    wire gpu_sel = !mem_fetch && mem_addr[31:7]==GPU_BASE[31:7];
    wire g3d_sel = !mem_fetch && mem_addr[31:13] == G3D_BASE[31:13];   // 8 KiB
    wire dma_window_sel = !mem_fetch && (mem_addr[31:3] == DMA_WINDOW_BASE[31:3]);
    wire none_sel = !(ram_sel || console_sel || done_sel || clint_sel || plic_sel || virtio_sel || rom_sel || input_sel || display_sel || palette_sel || fb_sel || simd_sel || gpu_sel || g3d_sel || dma_window_sel);

    assign gpu_valid = req && gpu_sel;
    assign g3d_valid = req && g3d_sel;
    assign dma_window_valid = req && dma_window_sel;
    assign simd_valid = req && simd_sel;
    assign ram_valid = req && ram_sel;
    assign console_valid = req && console_sel;
    assign done_valid = req && done_sel;
    assign clint_valid = req && clint_sel;
    assign plic_valid = req && plic_sel;
    assign virtio_valid = req && virtio_sel;
    assign rom_valid = req && rom_sel;
    assign input_valid = req && input_sel;
    assign display_valid = req && display_sel;
    assign palette_valid = req && palette_sel;
    assign fb_valid = req && fb_sel;

    assign mem_ready = req && ((ram_sel && ram_ready) || (console_sel && console_ready) ||
                               (done_sel && done_ready) || (clint_sel && clint_ready) || (plic_sel && plic_ready) || (virtio_sel && virtio_ready) || (rom_sel && rom_ready) ||
                               (input_sel && input_ready) || (display_sel && display_ready) || (palette_sel && palette_ready) ||
                               (fb_sel && fb_ready) || (simd_sel && simd_ready) || (gpu_sel && gpu_ready) || (g3d_sel && g3d_ready) ||
                               (dma_window_sel && dma_window_ready) || none_sel);
    assign mem_error = (ram_sel && ram_error) || (console_sel && console_error) ||
                       (done_sel && done_error) || (clint_sel && clint_error) || (plic_sel && plic_error) || (virtio_sel && virtio_error) || (rom_sel && rom_error) ||
                       (input_sel && input_error) || (display_sel && display_error) || (palette_sel && palette_error) ||
                       (fb_sel && fb_error) || (simd_sel && simd_error) || (gpu_sel && gpu_error) || (g3d_sel && g3d_error) ||
                       (dma_window_sel && dma_window_error) || none_sel;
    assign mem_rdata = ({32{ram_sel}} & ram_rdata) | ({32{console_sel}} & console_rdata) |
                       ({32{done_sel}} & done_rdata) | ({32{clint_sel}} & clint_rdata) | ({32{plic_sel}} & plic_rdata) | ({32{virtio_sel}} & virtio_rdata) | ({32{rom_sel}} & rom_rdata) |
                       ({32{input_sel}} & input_rdata) | ({32{display_sel}} & display_rdata) | ({32{palette_sel}} & palette_rdata) |
                       ({32{fb_sel}} & fb_rdata) | ({32{simd_sel}} & simd_rdata) | ({32{gpu_sel}} & gpu_rdata) |
                       ({32{g3d_sel}} & g3d_rdata) | ({32{dma_window_sel}} & dma_window_rdata);

    // `mem_we` is routed to the slaves by the machine, not decoded here: a write to a
    // read-only register is the slave's refusal, so the decoder stays direction-blind.
    wire unused_ok = &{1'b0, mem_we};
endmodule
