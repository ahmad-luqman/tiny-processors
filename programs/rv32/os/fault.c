/* fault: a program that goes wrong on purpose (Track 2). The kernel must
 * kill it with the right cause and keep the shell running.
 *
 *   fault load      a load from 0x0020_0000, unmapped on QEMU virt and on our machine (cause 5)
 *   fault illegal   an illegal instruction (cause 2)
 *   fault kernel    a store to the kernel's memory, refused by PMP (O5, cause 7)
 *   fault shell     a store to the shell's slot, another process's memory (O5, cause 7)
 *   fault csr       a read of mstatus, a machine CSR, from user mode (O5, cause 2)
 *   fault read      a load from the kernel's memory, refused by PMP (O5, cause 5)
 *   fault exec      a jump into the kernel's code, a fetch PMP refuses (O5, cause 1)
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
    if (!u_strcmp(args, "kernel")) {
        *(volatile uint32_t *)(OS_SLOT_BASE - OS_KERNEL_SIZE) = 0; /* the kernel's first word */
    }
    if (!u_strcmp(args, "shell")) {
        *(volatile uint32_t *)OS_SLOT_BASE = 0; /* slot 0: the shell's first word */
    }
    if (!u_strcmp(args, "csr")) {
        uint32_t mstatus;
        __asm__ volatile("csrr %0, mstatus" : "=r"(mstatus));
        return (int)mstatus;
    }
    if (!u_strcmp(args, "read")) {
        return (int)*(volatile uint32_t *)(OS_SLOT_BASE - OS_KERNEL_SIZE);
    }
    if (!u_strcmp(args, "exec")) {
        ((void (*)(void))(uintptr_t)(OS_SLOT_BASE - OS_KERNEL_SIZE))(); /* the kernel's _start */
    }
    u_puts("fault: load | illegal | kernel | shell | csr | read | exec\n");
    return 1;
}
