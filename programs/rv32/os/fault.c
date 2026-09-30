/* fault: a program that goes wrong on purpose (Track 2). The kernel must
 * kill it with the right cause and keep the shell running.
 *
 *   fault load      a load from 0x0020_0000, unmapped on QEMU virt and on our machine (cause 5)
 *   fault illegal   an illegal instruction (cause 2)
 */
#include "ulib.h"

int main(const char *args)
{
    if (!u_strcmp(args, "load")) {
        return (int)*(volatile uint32_t *)0x00200000u;
    }
    if (!u_strcmp(args, "illegal")) {
        __asm__ volatile("unimp"); /* csrrw x0, cycle, x0: a write to a read-only CSR */
    }
    u_puts("fault: load | illegal\n");
    return 1;
}
