/* diskprog: a program that lives on the disk, not in the RAM disk (issue #35). Its image carries
 * 256 KiB of words from a linear congruential sequence (diskprog_table.S), more than the kernel's
 * image has room for, and it runs in slots of the RAM above 8 MiB. It checks every word against
 * the sequence, so a sector read short, out of order or into the wrong place fails it. */
#include "ulib.h"

#define WORDS 65536u

extern const uint32_t diskprog_table[WORDS];

int main(const char *args)
{
    uint32_t value = 1, bad = 0;
    for (uint32_t i = 0; i < WORDS; i++) {
        bad += diskprog_table[i] != value;
        value = value * 1103515245u + 12345u;
    }
    u_puts("diskprog: ");
    u_putdec(WORDS);
    u_puts(" words at ");
    u_puthex((uint32_t)(uintptr_t)diskprog_table);
    u_puts(bad ? ", wrong: " : ", all right");
    if (bad) {
        u_putdec(bad);
    }
    if (*args) {
        u_puts(", args: ");
        u_puts(args);
    }
    u_puts("\n");
    return bad ? 1 : 0;
}
