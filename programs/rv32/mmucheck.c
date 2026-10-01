/* S-mode and Sv32 check (issue #20): one image for QEMU's virt board (with
 * `-cpu rv32,svade=on,svadu=off`, so a clear A or D bit faults as ours does),
 * our emulator and the RTL.
 *
 * Machine mode builds the page tables and runs small probes in S or U mode
 * through mret or sret. A probe does one thing (a load, a store, a jump, a
 * CSR read, sfence.vma, sret, or a spin while an interrupt is pending) and
 * then ecalls, or the jump or sret lands on code that does. Whatever ends it,
 * the ecall or a fault, reaches the machine-mode vector below (a delegated
 * trap reaches the S vector first, which records the S CSRs and ecalls). The
 * vector records mcause, mtval, mepc, a0 and mstatus and resumes the C code
 * in machine mode. Each check compares the
 * cause (and, where it matters, tval or the value read) with what the
 * privileged spec requires, and folds them into the PASS word, so QEMU, the
 * emulator and the RTL print the same word. Nothing depends on time.
 *
 * The map (satp.MODE = Sv32):
 * - 0x8000_0000: a 4 MiB megapage onto itself, RWX, supervisor only: the
 *   image, the stack, the tables. S-mode code and the S handler run here.
 * - 0x4000_0000: the same 4 MiB with U set: U-mode probes run here.
 * - 0x1000_0000: a level-0 table of 4 KiB test pages, one per case.
 * - 0x1040_0000: a megapage whose PPN is not 4 MiB aligned (a page fault).
 * - 0x1080_0000: a megapage at physical 2^32 (an access fault).
 * - 0x10c0_0000: a pointer with A set (reserved: a page fault).
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "csr.h"

#define PTE_V 0x01u
#define PTE_R 0x02u
#define PTE_W 0x04u
#define PTE_X 0x08u
#define PTE_U 0x10u
#define PTE_A 0x40u
#define PTE_D 0x80u

#define MODE_U 0u
#define MODE_S 1u
#define MODE_M 3u
#define WITH_MPRV 0x100u /* probe(): set mstatus.MPRV as the probe starts */

#define ECALL_U 8u
#define ECALL_S 9u
#define ECALL_M 11u
#define ILLEGAL 2u
#define FETCH_PAGE 12u
#define LOAD_PAGE 13u
#define STORE_PAGE 15u
#define LOAD_FAULT 5u

#define TEST_VA 0x10000000u
#define USER_ALIAS 0x40000000u /* VA of the image's U-mode alias */

enum page { P_R, P_RW, P_RW_CLEAN, P_X, P_UNACCESSED, P_INVALID, P_USER, P_W_ONLY, P_USER_CODE, P_CODE, P_POINTER,
            PAGES };

static uint32_t root[1024] __attribute__((aligned(4096)));
static uint32_t level0[1024] __attribute__((aligned(4096)));
static uint32_t pages[PAGES][1024] __attribute__((aligned(4096)));

/* Written by the vectors below, read here. */
volatile uint32_t seen[5];   /* mcause, mtval, mepc, a0 and mstatus at the trap */
volatile uint32_t s_seen[3]; /* scause, stval, sstatus as the S handler found them */
uint32_t probe_sp;

uint32_t probe(uint32_t mode, void (*fn)(void), uint32_t a0, uint32_t a1);
void m_vector(void), s_vector(void);
void p_load(void), p_store(void), p_jump(void), p_satp(void), p_sfence(void), p_sret(void), p_spin(void);

__asm__(
    "    .section .text, \"ax\", @progbits\n"
    "    .balign 4\n"
    /* probe(mode, fn, a0, a1): run fn in `mode` (bits 1:0, with MPRV if WITH_MPRV is set) with a0
     * and a1; return mcause. MPRV is set last, just before mret, since it applies to machine-mode
     * loads and stores at once. */
    "    .globl probe\n"
    "probe:\n"
    "    addi sp, sp, -16\n"
    "    sw ra, 12(sp)\n"
    "    la t0, probe_sp\n"
    "    sw sp, 0(t0)\n"
    "    li t0, 0x1800\n"
    "    csrc mstatus, t0\n"
    "    andi t1, a0, 3\n"
    "    slli t1, t1, 11\n"
    "    csrs mstatus, t1\n"
    "    csrw mepc, a1\n"
    "    andi t1, a0, 0x100\n"
    "    slli t1, t1, 9\n"          /* bit 8 to MPRV, bit 17 */
    "    csrs mstatus, t1\n"
    "    mv a0, a2\n"
    "    mv a1, a3\n"
    "    mret\n"
    "probe_resume:\n"
    "    la t0, probe_sp\n"
    "    lw sp, 0(t0)\n"
    "    lw ra, 12(sp)\n"
    "    addi sp, sp, 16\n"
    "    la t0, seen\n"
    "    lw a0, 0(t0)\n"
    "    ret\n"
    /* The machine vector: record the trap, then resume probe() in machine mode. */
    "    .balign 4\n"
    "    .globl m_vector\n"
    "m_vector:\n"
    "    la t1, seen\n"
    "    csrr t0, mcause\n"
    "    sw t0, 0(t1)\n"
    "    csrr t0, mtval\n"
    "    sw t0, 4(t1)\n"
    "    csrr t0, mepc\n"
    "    sw t0, 8(t1)\n"
    "    sw a0, 12(t1)\n"
    "    csrr t0, mstatus\n"
    "    sw t0, 16(t1)\n"
    "    li t0, 0x20000\n"          /* MPRV off: the C code resumes untranslated */
    "    csrc mstatus, t0\n"
    "    li t0, 0x1800\n"
    "    csrs mstatus, t0\n"
    "    la t0, probe_resume\n"
    "    csrw mepc, t0\n"
    "    mret\n"
    /* The supervisor vector: record what S mode sees, then hand back to machine mode. */
    "    .balign 4\n"
    "    .globl s_vector\n"
    "s_vector:\n"
    "    la t1, s_seen\n"
    "    csrr t0, scause\n"
    "    sw t0, 0(t1)\n"
    "    csrr t0, stval\n"
    "    sw t0, 4(t1)\n"
    "    csrr t0, sstatus\n"
    "    sw t0, 8(t1)\n"
    "    ecall\n"
    /* Probes: position independent, so U mode runs them through the alias. */
    "    .balign 4\n"
    "    .globl p_load, p_store, p_jump, p_satp, p_sfence, p_sret, p_spin\n"
    "p_load:   lw a0, 0(a0)\n"
    "          ecall\n"
    "p_store:  sw a1, 0(a0)\n"
    "          ecall\n"
    "p_jump:   jr a0\n"
    "p_satp:   csrr a0, satp\n"
    "          ecall\n"
    "p_sfence: sfence.vma\n"
    "          ecall\n"
    "p_sret:   sret\n"
    "p_spin:   csrsi sstatus, 2\n" /* SIE */
    "1:        j 1b\n");

static uint32_t checksum = 2166136261u;
static uint32_t failures, checks;

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u; /* FNV-1a, as the other checks */
}

static void check(const char *what, uint32_t got, uint32_t want)
{
    checks++;
    fold(got);
    if (got != want) {
        rv32_puts("mmucheck: FAILED ");
        rv32_puts(what);
        rv32_puts(" got ");
        rv32_put_hex32(got);
        rv32_puts(" want ");
        rv32_put_hex32(want);
        rv32_putc('\n');
        failures++;
    }
}

static uint32_t pte(const void *target, uint32_t flags)
{
    return (uint32_t)(uintptr_t)target >> 12 << 10 | flags;
}

static uint32_t va(enum page p)
{
    return TEST_VA + 4096u * (uint32_t)p;
}

static void (*user(void (*fn)(void)))(void)
{
    return (void (*)(void))((uintptr_t)fn - 0x80000000u + USER_ALIAS);
}

static void flush(void)
{
    __asm__ volatile("sfence.vma" ::: "memory");
}

static void build_tables(void)
{
    root[0x80000000u >> 22] = pte((void *)0x80000000u, PTE_V | PTE_R | PTE_W | PTE_X | PTE_A | PTE_D);
    root[USER_ALIAS >> 22] = pte((void *)0x80000000u, PTE_V | PTE_R | PTE_W | PTE_X | PTE_U | PTE_A | PTE_D);
    root[TEST_VA >> 22] = pte(level0, PTE_V);
    root[(TEST_VA >> 22) + 1] = pte((void *)0x80001000u, PTE_V | PTE_R | PTE_A); /* misaligned megapage */
    root[(TEST_VA >> 22) + 2] = 0x100000u << 10 | PTE_V | PTE_R | PTE_A;         /* a megapage at 2^32 */
    root[(TEST_VA >> 22) + 3] = pte(level0, PTE_V | PTE_A);                      /* a pointer with A set */
    static const uint32_t flags[PAGES] = {
        [P_R] = PTE_V | PTE_R | PTE_A,
        [P_RW] = PTE_V | PTE_R | PTE_W | PTE_A | PTE_D,
        [P_RW_CLEAN] = PTE_V | PTE_R | PTE_W | PTE_A,
        [P_X] = PTE_V | PTE_X | PTE_A,
        [P_UNACCESSED] = PTE_V | PTE_R | PTE_W | PTE_D,
        [P_INVALID] = PTE_R | PTE_W | PTE_A | PTE_D,
        [P_USER] = PTE_V | PTE_R | PTE_W | PTE_U | PTE_A | PTE_D,
        [P_W_ONLY] = PTE_V | PTE_W | PTE_A | PTE_D,
        [P_USER_CODE] = PTE_V | PTE_X | PTE_U | PTE_A,
        [P_CODE] = PTE_V | PTE_X | PTE_A,
        [P_POINTER] = PTE_V, /* a pointer where a leaf must be */
    };
    for (uint32_t p = 0; p < PAGES; p++) {
        level0[p] = pte(pages[p], flags[p]);
        pages[p][0] = 0x1000u * p + 0xa5u; /* what a load of the page's first word returns */
    }
    pages[P_USER_CODE][0] = 0x00000073u; /* ecall */
    pages[P_CODE][0] = 0x00000073u;
    csr_write(0x180, 0x80000000u | (uint32_t)(uintptr_t)root >> 12); /* satp: Sv32 */
    flush();
}

/* A load in `mode`; returns the cause and leaves the value read in seen[3]. */
static uint32_t load(uint32_t mode, uint32_t address)
{
    return probe(mode, mode == MODE_U ? user(p_load) : p_load, address, 0);
}

static uint32_t store(uint32_t mode, uint32_t address, uint32_t value)
{
    return probe(mode, mode == MODE_U ? user(p_store) : p_store, address, value);
}

static uint32_t jump(uint32_t mode, uint32_t address)
{
    return probe(mode, mode == MODE_U ? user(p_jump) : p_jump, address, 0);
}

int main(void)
{
    csr_write(CSR_MTVEC, (uint32_t)(uintptr_t)m_vector);
    /* PMP: one entry that grants everything, since S and U mode need a match (QEMU as ours). */
    csr_write(CSR_PMPADDR0, 0xffffffffu);
    csr_write(CSR_PMPCFG0, PMP_NAPOT | PMP_R | PMP_W | PMP_X);
    csr_write(CSR_MCOUNTEREN, 0);

    /* 1. The CSRs: MPP holds S, satp holds the root, medeleg holds a load page fault. */
    csr_write(CSR_MSTATUS, 1u << 11);
    check("mstatus.MPP holds S", (csr_read(CSR_MSTATUS) >> 11) & 3u, MODE_S);
    csr_write(0x302, 1u << LOAD_PAGE);
    check("medeleg holds bit 13", csr_read(0x302) & 0xffffu, 1u << LOAD_PAGE);
    csr_write(0x302, 0);
    csr_write(0x105, (uint32_t)(uintptr_t)s_vector); /* stvec */
    check("stvec", csr_read(0x105), (uint32_t)(uintptr_t)s_vector);
    build_tables();
    check("satp", csr_read(0x180), 0x80000000u | (uint32_t)(uintptr_t)root >> 12);

    /* 2. S-mode reads and writes by permission. */
    check("S load R", load(MODE_S, va(P_R)), ECALL_S);
    check("S load R value", seen[3], 0xa5u);
    check("S store R", store(MODE_S, va(P_R), 1), STORE_PAGE);
    check("S store R tval", seen[1], va(P_R));
    check("S store RW", store(MODE_S, va(P_RW) + 8, 0x12345678u), ECALL_S);
    check("S store RW landed", pages[P_RW][2], 0x12345678u);
    check("S store D clear", store(MODE_S, va(P_RW_CLEAN), 1), STORE_PAGE);
    check("S load D clear", load(MODE_S, va(P_RW_CLEAN)), ECALL_S);
    check("S load X only", load(MODE_S, va(P_X)), LOAD_PAGE);
    csr_set(CSR_MSTATUS, 1u << 19); /* MXR */
    check("S load X with MXR", load(MODE_S, va(P_X)), ECALL_S);
    csr_clear(CSR_MSTATUS, 1u << 19);
    check("S load A clear", load(MODE_S, va(P_UNACCESSED)), LOAD_PAGE);
    check("S load invalid", load(MODE_S, va(P_INVALID) + 4), LOAD_PAGE);
    check("S load invalid tval", seen[1], va(P_INVALID) + 4);
    check("S load W only", load(MODE_S, va(P_W_ONLY)), LOAD_PAGE);
    check("S load pointer at level 0", load(MODE_S, va(P_POINTER)), LOAD_PAGE);
    check("S load misaligned megapage", load(MODE_S, TEST_VA + 0x400000u), LOAD_PAGE);
    check("S load unmapped", load(MODE_S, 0x20000000u), LOAD_PAGE);
    /* Not here: a misaligned load in an invalid page. Ours checks alignment first; the privileged
     * spec allows either order (docs/rv32.md "Sv32"), and QEMU translates first and page-faults. */
    check("S load past 32-bit physical", load(MODE_S, TEST_VA + 0x800000u), LOAD_FAULT);
    check("S load past 32-bit physical tval", seen[1], TEST_VA + 0x800000u);
    check("S load through a pointer with A", load(MODE_S, TEST_VA + 0xc00000u), LOAD_PAGE);
    check("S load user page", load(MODE_S, va(P_USER)), LOAD_PAGE);
    csr_set(CSR_MSTATUS, 1u << 18); /* SUM */
    check("S load user page with SUM", load(MODE_S, va(P_USER)), ECALL_S);
    csr_clear(CSR_MSTATUS, 1u << 18);

    /* 3. Fetches. */
    check("S fetch X", jump(MODE_S, va(P_CODE)), ECALL_S);
    check("S fetch user page", jump(MODE_S, va(P_USER_CODE)), FETCH_PAGE);
    csr_set(CSR_MSTATUS, 1u << 18);
    check("S fetch user page with SUM", jump(MODE_S, va(P_USER_CODE)), FETCH_PAGE);
    csr_clear(CSR_MSTATUS, 1u << 18);
    check("S fetch R only", jump(MODE_S, va(P_R)), FETCH_PAGE);
    check("S fetch R only tval", seen[1], va(P_R));

    /* 4. User mode. */
    check("U load user page", load(MODE_U, va(P_USER)), ECALL_U);
    check("U load user value", seen[3], 0x1000u * P_USER + 0xa5u);
    check("U load supervisor page", load(MODE_U, va(P_R)), LOAD_PAGE);
    check("U fetch user code", jump(MODE_U, va(P_USER_CODE)), ECALL_U);
    check("U fetch supervisor code", jump(MODE_U, va(P_CODE)), FETCH_PAGE);
    check("U satp", probe(MODE_U, user(p_satp), 0, 0), ILLEGAL);
    check("U sfence.vma", probe(MODE_U, user(p_sfence), 0, 0), ILLEGAL);
    check("S satp", probe(MODE_S, p_satp, 0, 0), ECALL_S);
    check("S satp value", seen[3], csr_read(0x180));
    check("S sfence.vma", probe(MODE_S, p_sfence, 0, 0), ECALL_S);

    /* 5. TVM and TSR. */
    csr_set(CSR_MSTATUS, 1u << 20);
    check("S satp with TVM", probe(MODE_S, p_satp, 0, 0), ILLEGAL);
    check("S sfence.vma with TVM", probe(MODE_S, p_sfence, 0, 0), ILLEGAL);
    csr_clear(CSR_MSTATUS, 1u << 20);
    csr_set(CSR_MSTATUS, 1u << 22);
    check("S sret with TSR", probe(MODE_S, p_sret, 0, 0), ILLEGAL);
    csr_clear(CSR_MSTATUS, 1u << 22);

    /* 6. MPRV: machine-mode loads run at MPP's privilege. probe() enters the probe in machine mode
     * through mret, which leaves MPP = U, so the probe's load is a user-mode one; its trap (back in
     * machine mode, MPP = M) is untranslated again. An mret to a lower mode clears MPRV. */
    check("M load with MPRV of a user page", probe(MODE_M | WITH_MPRV, p_load, va(P_USER), 0), ECALL_M);
    check("M load with MPRV value", seen[3], 0x1000u * P_USER + 0xa5u);
    check("M load with MPRV of a supervisor page", probe(MODE_M | WITH_MPRV, p_load, va(P_R), 0), LOAD_PAGE);
    check("M load without MPRV", probe(MODE_M, p_load, (uint32_t)(uintptr_t)&pages[P_R][0], 0), ECALL_M);
    check("mret to S clears MPRV", probe(MODE_S | WITH_MPRV, p_satp, 0, 0) == ECALL_S && !((seen[4] >> 17) & 1u), 1);

    /* 7. sret from machine mode into U mode, then a delegated page fault taken in S mode. */
    csr_write(0x141, (uint32_t)(uintptr_t)user(p_load)); /* sepc */
    csr_write(0x302, 1u << LOAD_PAGE);                    /* medeleg */
    csr_clear(CSR_MSTATUS, 1u << 8);                      /* SPP = U */
    csr_set(CSR_MSTATUS, 1u << 5);                        /* SPIE */
    check("U load via sret, delegated", probe(MODE_M, p_sret, va(P_INVALID), 0), ECALL_S);
    check("scause", s_seen[0], LOAD_PAGE);
    check("stval", s_seen[1], va(P_INVALID));
    check("sstatus.SPP is U, SPIE took SIE", s_seen[2] & 0x122u, 0x020u);
    check("S load delegated from S", load(MODE_S, va(P_INVALID)), ECALL_S);
    check("sstatus.SPP is S", s_seen[2] & 0x100u, 0x100u);
    csr_write(0x302, 0);

    /* 8. A delegated supervisor software interrupt, raised by machine mode. */
    csr_write(0x303, 1u << 1);           /* mideleg: SSI */
    csr_write(CSR_MIE, 1u << 1);         /* SSIE */
    csr_set(CSR_MIP, 1u << 1);           /* SSIP */
    check("S interrupt delegated", probe(MODE_S, p_spin, 0, 0), ECALL_S);
    check("S interrupt scause", s_seen[0], 0x80000001u);
    csr_clear(CSR_MIP, 1u << 1);
    csr_write(CSR_MIE, 0);
    csr_write(0x303, 0);

    csr_write(0x180, 0); /* translation off */
    flush();
    rv32_puts("mmucheck: ");
    rv32_put_udec(checks);
    rv32_puts(" checks\n");
    if (failures) {
        rv32_exit(1);
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    rv32_exit(0);
}
