/* dmaprobe: the DMA window from a program that drives the engines (issue #20).
 *
 * The program is flagged `accelerators`, so PMP lets it at G1's and G2's registers, and the
 * kernel sets the DMA window to its own slot. The engines must refuse to reach the kernel or
 * another program's slot through their DMA:
 *
 *   dmaprobe          a G1 blit from its own memory is accepted; blits from the kernel and from
 *                     the shell's slot, and G2 depth-buffer clears at either, are refused
 *   dmaprobe window   a load of the window's START register, which PMP keeps from programs
 *                     (cause 5)
 *
 * The blits have no destination pixels (W = H = 0): G1 still validates the source, so each
 * reports DONE or INVALID without touching the framebuffer. The refused clears write nothing.
 * The load is written in assembly in the section user.ld places after the entry code, so the
 * fault's pc is the same whatever compiler built the program.
 */
#include "ulib.h"

#include "board.h"
#include "g3d.h"
#include "gpu.h"
#include "mmio.h"

#define KERNEL 0x80040000u    /* in the kernel's MiB: the bare machine's G3D_DEMO_ZBASE, which the menu used until issue #20 */
#define SHELL  0x80100000u    /* slot 0 */

void dmaprobe_window(void);

__asm__(
    "    .pushsection .text.fault, \"ax\", @progbits\n"
    "    .balign 4\n"
    "    .globl dmaprobe_window\n"
    "dmaprobe_window: lui t0, 0x1100a\n"    /* the DMA window's START */
    "                 lw a0, 0(t0)\n"
    "                 ret\n"
    "    .popsection\n");

static uint8_t source[16];

/* G1 with a zero-sized destination: only SETUP runs, and it judges the source. */
static uint32_t blit(uint32_t address)
{
    static const uint32_t shape[GP_COUNT] = {[GP_OP] = GPU_BLIT, [GP_STRIDE] = 4, [GP_SW] = 4, [GP_SH] = 4};
    for (uint32_t i = 0; i < GP_COUNT; i++) {
        mmio_write32(GPU_BASE + GPU_PARAMS + 4 * i, i == GP_SRC ? address : shape[i]);
    }
    mmio_write32(GPU_BASE + GPU_COMMAND, GPU_START);
    while (mmio_read32(GPU_BASE + GPU_STATUS) == GPU_BUSY) {
    }
    return mmio_read32(GPU_BASE + GPU_STATUS) == GPU_DONE ? 0 : mmio_read32(GPU_BASE + GPU_ERROR);
}

/* G2's CLEAR_Z validates the depth buffer before it writes a word of it. */
static uint32_t clear(uint32_t zbase)
{
    mmio_write32(G3D_BASE + G3D_ZBASE, zbase);
    mmio_write32(G3D_BASE + G3D_COMMAND, G3D_CLEAR_Z);
    while (mmio_read32(G3D_BASE + G3D_STATUS) == G3D_BUSY) {
    }
    return mmio_read32(G3D_BASE + G3D_STATUS) == G3D_DONE ? 0 : mmio_read32(G3D_BASE + G3D_ERROR);
}

static void report(const char *what, uint32_t error)
{
    u_puts("dmaprobe: ");
    u_puts(what);
    u_puts(error ? " refused, error " : " accepted\n");
    if (error) {
        u_putdec(error);
        u_puts("\n");
    }
}

int main(const char *args)
{
    if (!u_strcmp(args, "window")) {
        dmaprobe_window();
        u_puts("dmaprobe: the window was readable\n");
        return 1;
    }
    report("G1 blit from its own memory", blit((uint32_t)source));
    report("G1 blit from the kernel", blit(KERNEL));
    report("G1 blit from the shell", blit(SHELL));
    report("G2 depth buffer in the kernel", clear(KERNEL));
    report("G2 depth buffer in the shell", clear(SHELL));
    return 0;
}
