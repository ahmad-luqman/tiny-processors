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
 *
 * The faulting instructions are written in assembly in their own section,
 * which user.ld places right after the entry code, so every fault's pc (and
 * `csr`'s instruction word, which names its register) is the same whatever
 * compiler built the program, and the transcripts can pin them.
 */
#include "ulib.h"

void fault_load(void), fault_illegal(void), fault_kernel(void), fault_shell(void), fault_csr(void), fault_read(void),
    fault_exec(void);

__asm__(
    "    .pushsection .text.fault, \"ax\", @progbits\n"
    "    .balign 4\n"
    "    .globl fault_load, fault_illegal, fault_kernel, fault_shell, fault_csr, fault_read, fault_exec\n"
    "fault_load:    lui t0, 0x200\n"        /* 0x0020_0000 */
    "               lw a0, 0(t0)\n"
    "               ret\n"
    "fault_illegal: unimp\n"                 /* csrrw x0, cycle, x0: a write to a read-only CSR */
    "               ret\n"
    "fault_kernel:  lui t0, 0x80000\n"      /* the kernel's first word */
    "               sw zero, 0(t0)\n"
    "               ret\n"
    "fault_shell:   lui t0, 0x80100\n"      /* slot 0: the shell's first word */
    "               sw zero, 0(t0)\n"
    "               ret\n"
    "fault_csr:     csrr a0, mstatus\n"
    "               ret\n"
    "fault_read:    lui t0, 0x80000\n"
    "               lw a0, 0(t0)\n"
    "               ret\n"
    "fault_exec:    lui t0, 0x80000\n"      /* the kernel's _start */
    "               jr t0\n"
    "    .popsection\n");

int main(const char *args)
{
    static const struct {
        const char *name;
        void (*run)(void);
    } faults[] = {
        {"load", fault_load}, {"illegal", fault_illegal}, {"kernel", fault_kernel}, {"shell", fault_shell},
        {"csr", fault_csr},   {"read", fault_read},       {"exec", fault_exec},
    };
    for (uint32_t i = 0; i < sizeof faults / sizeof faults[0]; i++) {
        if (!u_strcmp(args, faults[i].name)) {
            faults[i].run();
        }
    }
    u_puts("fault: load | illegal | kernel | shell | csr | read | exec\n");
    return 1;
}
