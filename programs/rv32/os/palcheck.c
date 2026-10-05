/* palcheck: the palette through the kernel (issue #35). It reads the 256 entries and checks they
 * are the RGB332 mapping, sets a palette whose words carry a top byte, reads it back with that
 * byte dropped, puts the power-on palette back, and checks that a misaligned buffer and an
 * unknown direction are refused. QEMU has no display, so there the call is an error and that
 * is all it prints. It lives on the disk, beside diskprog. */
#include "ulib.h"

static uint32_t colours[OS_PALETTE_ENTRIES], saved[OS_PALETTE_ENTRIES];

static uint32_t rgb332(uint32_t p)
{
    return ((p >> 5 & 7u) * 255u / 7u) << 16 | ((p >> 2 & 7u) * 255u / 7u) << 8 | (p & 3u) * 255u / 3u;
}

int main(const char *args)
{
    (void)args;
    if (sys_get_palette(saved) == SYS_ERROR) {
        u_puts("palcheck: no palette\n");
        return 0;
    }
    uint32_t bad = 0;
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        bad += saved[i] != rgb332(i);
        colours[i] = 0xa5000000u | i * 0x010203u;
    }
    u_puts(bad ? "palcheck: power-on palette wrong\n" : "palcheck: power-on palette is RGB332\n");
    uint32_t set = sys_set_palette(colours);
    uint32_t got = sys_get_palette(colours);
    uint32_t wrong = 0;
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        wrong += colours[i] != ((i * 0x010203u) & 0x00ffffffu);
    }
    u_puts(set == 0 && got == 0 && !wrong ? "palcheck: set and read back, top byte dropped\n" : "palcheck: read back wrong\n");
    uint32_t restored = sys_set_palette(saved);
    uint32_t refused = syscall3(SYS_PALETTE, (uint32_t)(uintptr_t)colours + 2u, 0, 0) == SYS_ERROR &&
                       syscall3(SYS_PALETTE, (uint32_t)(uintptr_t)colours, 2, 0) == SYS_ERROR &&
                       syscall3(SYS_PALETTE, 0x80000000u, 0, 0) == SYS_ERROR;
    u_puts(restored == 0 && refused ? "palcheck: restored; bad requests refused\n" : "palcheck: refusals wrong\n");
    return bad || wrong || restored || !refused;
}
