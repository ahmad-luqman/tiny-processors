/* palcheck: the palette through the kernel (issue #35). It reads the 256 entries and checks they
 * are the RGB332 mapping, sets a palette whose words carry a top byte, reads it back with that
 * byte dropped, puts the power-on palette back and reads that back too. It checks five refusals:
 * a misaligned buffer, an unknown direction, memory that is not the program's, a buffer that runs
 * past the top of its memory, and one that runs into its stack's guard page. `palcheck leave` sets
 * a palette and exits without restoring it, so the next `palcheck` shows the kernel restored it.
 * QEMU has no display, so there the call is an error and that is all it prints; on a machine with
 * a display an error is a failure. It lives on the disk, beside diskprog. */
#include "ulib.h"

static uint32_t colours[OS_PALETTE_ENTRIES], saved[OS_PALETTE_ENTRIES];
extern char _stack_bottom[], _stack_top[]; /* user.ld: the stack, whose lowest page is the guard */

/* RGB332's word for a pixel value; the same table is in tools/rv32emu_core.c,
 * rtl/rv32/rv32_palette.v and tools/rv32_devices.py, and the tests compare them. */
static uint32_t rgb332(uint32_t p)
{
    return ((p >> 5 & 7u) * 255u / 7u) << 16 | ((p >> 2 & 7u) * 255u / 7u) << 8 | (p & 3u) * 255u / 3u;
}

static uint32_t is_rgb332(const uint32_t *words)
{
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        if (words[i] != rgb332(i)) {
            return 0;
        }
    }
    return 1;
}

/* A request the kernel must refuse; syscall3 directly, since the wrappers would not pass it. */
static uint32_t refused(uint32_t address, uint32_t direction)
{
    return syscall3(SYS_PALETTE, address, direction, 0) == SYS_ERROR;
}

int main(const char *args)
{
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        colours[i] = 0xa5000000u | i * 0x010203u;
    }
    if (sys_get_palette(saved) == SYS_ERROR) {
        u_puts(sys_display() ? "palcheck: the display has no palette\n" : "palcheck: no palette\n");
        return sys_display() != 0;
    }
    if (args[0] == 'l') { /* leave */
        uint32_t set = sys_set_palette(colours);
        u_puts(set == 0 ? "palcheck: palette set and left\n" : "palcheck: could not set the palette\n");
        return set != 0;
    }
    uint32_t power_on = is_rgb332(saved);
    u_puts(power_on ? "palcheck: palette is RGB332\n" : "palcheck: palette is not RGB332\n");
    uint32_t set = sys_set_palette(colours), got = sys_get_palette(colours), wrong = 0;
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        wrong += colours[i] != ((i * 0x010203u) & 0x00ffffffu);
    }
    u_puts(set == 0 && got == 0 && !wrong ? "palcheck: set and read back, top byte dropped\n" : "palcheck: read back wrong\n");
    uint32_t restored = sys_set_palette(saved) == 0 && sys_get_palette(colours) == 0 && is_rgb332(colours);
    uint32_t top = (uint32_t)(uintptr_t)_stack_top, guard = (uint32_t)(uintptr_t)_stack_bottom;
    uint32_t refusals = refused((uint32_t)(uintptr_t)colours + 2u, OS_PALETTE_READ) &&
                        refused((uint32_t)(uintptr_t)colours, 2) && refused(0x80000000u, OS_PALETTE_READ) &&
                        refused(top - 512u, OS_PALETTE_READ) && refused(guard - 512u, OS_PALETTE_READ);
    u_puts(restored && refusals ? "palcheck: restored; bad requests refused\n" : "palcheck: restore or refusals wrong\n");
    return !power_on || set || got || wrong || !restored || !refusals;
}
