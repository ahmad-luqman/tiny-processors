/* fault: a program that goes wrong on purpose (Track 2). The kernel must
 * kill it with the right cause and keep the shell running.
 *
 *   fault load      a load from 0x0020_0000, where nothing is (cause 13; 5 before issue #25)
 *   fault illegal   an illegal instruction (cause 2)
 *   fault kernel    a store to the kernel's memory (O5; cause 15, 7 before issue #25)
 *   fault shell     a store to the shell's slot, another process's memory (O5; cause 15, 7 before)
 *   fault csr       a read of mstatus, a machine CSR, from user mode (O5, cause 2)
 *   fault read      a load from the kernel's memory (O5; cause 13, 5 before issue #25)
 *   fault exec      a jump into the kernel's code (O5; cause 12, 1 before issue #25)
 *   fault prev      a store to slot 2, where primes ran (issue #25, cause 15): run right after
 *                   primes, it takes the process table entry primes left, whose page table must
 *                   no longer map primes' slot
 *   fault engine    a load from G1's registers by a program not flagged `accelerators` (issue #25,
 *                   cause 13): run right after dmaprobe, which is, it checks those mappings went too
 *   fault tail      a load just past the framebuffer, in the last page its mapping covers: the
 *                   page table lets it through and PMP, the backstop, refuses it (issue #25,
 *                   cause 5); without a display it says so and exits 1
 *
 * Since issue #25 the page table is asked first, so every address above that
 * the process does not own is a page fault (13 load, 15 store, 12 fetch).
 *
 * The faulting instructions are written in assembly in their own section,
 * which user.ld places right after the entry code, so every fault's pc (and
 * `csr`'s instruction word, which names its register) is the same whatever
 * compiler built the program, and the transcripts can pin them.
 */
#include "ulib.h"

#include "board.h"

void fault_load(void), fault_illegal(void), fault_kernel(void), fault_shell(void), fault_csr(void), fault_read(void),
    fault_exec(void), fault_prev(void), fault_engine(void);
void fault_tail(uint32_t address);

__asm__(
    "    .pushsection .text.fault, \"ax\", @progbits\n"
    "    .balign 4\n"
    "    .globl fault_load, fault_illegal, fault_kernel, fault_shell, fault_csr, fault_read, fault_exec\n"
    "    .globl fault_prev, fault_engine, fault_tail\n"
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
    "fault_prev:    lui t0, 0x80140\n"      /* slot 2: primes' first word */
    "               sw zero, 0(t0)\n"
    "               ret\n"
    "fault_engine:  lui t0, 0x11007\n"      /* G1's COMMAND register */
    "               lw a0, 0(t0)\n"
    "               ret\n"
    "fault_tail:    lw a0, 0(a0)\n"         /* the address in a0 */
    "               ret\n"
    "    .popsection\n");

int main(const char *args)
{
    static const struct {
        const char *name;
        void (*run)(void);
    } faults[] = {
        {"load", fault_load}, {"illegal", fault_illegal}, {"kernel", fault_kernel}, {"shell", fault_shell},
        {"csr", fault_csr},   {"read", fault_read},       {"exec", fault_exec},  {"prev", fault_prev},
        {"engine", fault_engine},
    };
    if (!u_strcmp(args, "tail")) {
        uint8_t *framebuffer = sys_display();
        if (!framebuffer) {
            u_puts("fault: no display\n");
            return 1;
        }
        fault_tail((uint32_t)(uintptr_t)framebuffer + RV32_FB_SIZE);
    }
    for (uint32_t i = 0; i < sizeof faults / sizeof faults[0]; i++) {
        if (!u_strcmp(args, faults[i].name)) {
            faults[i].run();
        }
    }
    u_puts("fault: load | illegal | kernel | shell | csr | read | exec | prev | engine | tail\n");
    return 1;
}
