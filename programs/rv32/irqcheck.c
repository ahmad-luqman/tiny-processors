/* Interrupt check (Track 2, O1): one image for QEMU's virt board, our
 * emulator and the RTL.
 *
 * It finds the CLINT and the PLIC in the device tree (a1), installs
 * trap_entry.S, and checks, in order:
 *
 * 1. mscratch holds a word; mstatus.MIE/MPIE and mie's three enables are
 *    writable (only those bits are compared: QEMU's mstatus and mie have more).
 * 2. Gating: a pending timer interrupt (mtimecmp = 0) shows in mip but is not
 *    taken while mie or mstatus.MIE is clear; setting MIE takes it, the
 *    handler sees MIE clear and MPIE set, and mret sets MIE again.
 * 3. A software interrupt through msip.
 * 4. Priority: MSI before MTI when both are pending.
 * 5. wfi: a timer interrupt 2,000 ticks ahead wakes a wfi loop, and mtime has
 *    reached mtimecmp by then.
 * 6. The PLIC's registers: a priority and the threshold hold what is written,
 *    the enable word holds the input's source, nothing is pending and a claim
 *    returns 0.
 * 7. Where the tree lists our input device (not on QEMU): its PLIC source from
 *    the tree, a threshold that masks it, and then, with a software interrupt
 *    also pending, MEI taken before MSI, the handler claiming the source,
 *    draining the scripted events and completing it.
 *
 * The PASS word folds only what every platform shares (steps 1 to 6), so
 * QEMU, the emulator and the RTL print the same word. Interrupts land on a
 * backend-dependent instruction, so the RTL is compared with the emulator at
 * the results level, or trace for trace in the RTL's step-tick mode.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "csr.h"
#include "fdt.h"
#include "mmio.h"

#define INPUT_COMPATIBLE "tiny-processors,input"

extern void trap_entry(void);

static uint32_t checksum = 2166136261u;
static uint32_t failures;
static uint32_t clint, plic;

/* What the handler saw, in order. */
static volatile uint32_t taken[8];
static volatile uint32_t ntaken;
static volatile uint32_t handler_status;
static volatile uint32_t claimed_source, events;

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u; /* FNV-1a, as the other checks */
}

static void check(const char *what, uint32_t got, uint32_t want)
{
    if (got != want) {
        rv32_puts("irqcheck: FAILED ");
        rv32_puts(what);
        rv32_puts(" got ");
        rv32_put_hex32(got);
        rv32_puts(" want ");
        rv32_put_hex32(want);
        rv32_putc('\n');
        failures++;
    }
}

static void ok(const char *what)
{
    rv32_puts("irqcheck: ");
    rv32_puts(what);
    rv32_puts(" ok\n");
}

static void set_mtimecmp(uint64_t value)
{
    /* Low word to all ones first, so no intermediate value lies below the target. */
    mmio_write32(clint + RV32_CLINT_MTIMECMP, 0xffffffffu);
    mmio_write32(clint + RV32_CLINT_MTIMECMP + 4, (uint32_t)(value >> 32));
    mmio_write32(clint + RV32_CLINT_MTIMECMP, (uint32_t)value);
}

static uint64_t mtime(void)
{
    uint32_t hi, lo;
    do {
        hi = mmio_read32(clint + RV32_CLINT_MTIME + 4);
        lo = mmio_read32(clint + RV32_CLINT_MTIME);
    } while (hi != mmio_read32(clint + RV32_CLINT_MTIME + 4));
    return (uint64_t)hi << 32 | lo;
}

/* Called by trap_entry.S; returns the PC to resume at. Every interrupt is handled by removing
 * its cause, so returning to mepc does not take it again. */
uint32_t trap_handler(uint32_t cause, uint32_t tval, uint32_t epc)
{
    (void)tval;
    if (!(cause & MCAUSE_INTERRUPT)) {
        rv32_puts("irqcheck: FAILED unexpected exception ");
        rv32_put_hex32(cause);
        rv32_putc(' ');
        rv32_put_hex32(epc);
        rv32_putc('\n');
        rv32_exit(90);
    }
    uint32_t code = cause & 0x1fu;
    if (ntaken < 8) {
        taken[ntaken] = code;
    }
    ntaken = ntaken + 1;
    handler_status = csr_read(CSR_MSTATUS) & (MSTATUS_MIE | MSTATUS_MPIE);
    if (code == IRQ_MSI) {
        mmio_write32(clint + RV32_CLINT_MSIP, 0);
    } else if (code == IRQ_MTI) {
        set_mtimecmp(~0ull);
    } else if (code == IRQ_MEI) {
        uint32_t source = mmio_read32(plic + RV32_PLIC_CLAIM);
        for (uint32_t event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT); event;
             event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT)) {
            events = events + 1;
        }
        mmio_write32(plic + RV32_PLIC_CLAIM, source);
        claimed_source = source;
    } else {
        rv32_puts("irqcheck: FAILED unexpected interrupt ");
        rv32_put_udec(code);
        rv32_putc('\n');
        rv32_exit(91);
    }
    return epc;
}

static void reset_counts(void)
{
    ntaken = 0;
    handler_status = 0xffffffffu;
}

static void keyboard(const fdt *tree)
{
    uint32_t base, size, source;
    fdt_status status = fdt_find(tree, "compatible", INPUT_COMPATIBLE, 0, &base, &size);
    if (status == FDT_NOT_FOUND) {
        rv32_puts("irqcheck: keyboard absent\n");
        return;
    }
    status = status != FDT_OK ? status : fdt_cell(tree, "compatible", INPUT_COMPATIBLE, "interrupts", 0, &source);
    if (status != FDT_OK || base != RV32_INPUT_BASE || source == 0 || source > 31) {
        rv32_puts("irqcheck: FAILED input node, device tree status ");
        rv32_put_udec((uint32_t)status);
        rv32_putc('\n');
        failures++;
        return;
    }
    /* Priority 1 against threshold 1 is masked: pending, but no MEIP. */
    mmio_write32(plic + RV32_PLIC_PRIORITY(source), 1);
    mmio_write32(plic + RV32_PLIC_ENABLE, 1u << source);
    mmio_write32(plic + RV32_PLIC_THRESHOLD, 1);
    mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 1); /* frame 1: the script's events arrive */
    check("input pending", mmio_read32(plic + RV32_PLIC_PENDING) & (1u << source), 1u << source);
    check("threshold masks MEIP", csr_read(CSR_MIP) & MIP_MEIP, 0);
    mmio_write32(plic + RV32_PLIC_THRESHOLD, 0);
    check("MEIP", csr_read(CSR_MIP) & MIP_MEIP, MIP_MEIP);
    /* MEI outranks MSI. */
    reset_counts();
    events = 0;
    mmio_write32(clint + RV32_CLINT_MSIP, 1);
    csr_write(CSR_MIE, MIP_MEIP | MIP_MSIP);
    csr_set(CSR_MSTATUS, MSTATUS_MIE);
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, 0);
    check("keyboard interrupts", ntaken, 2);
    check("MEI first", taken[0], IRQ_MEI);
    check("then MSI", taken[1], IRQ_MSI);
    check("claimed source", claimed_source, source);
    check("events drained", events, 2);
    check("nothing pending after", mmio_read32(plic + RV32_PLIC_PENDING), 0);
    check("claim after", mmio_read32(plic + RV32_PLIC_CLAIM), 0);
    mmio_write32(plic + RV32_PLIC_ENABLE, 0);
    mmio_write32(plic + RV32_PLIC_PRIORITY(source), 0);
    rv32_puts("irqcheck: keyboard source ");
    rv32_put_udec(source);
    rv32_puts(" events ");
    rv32_put_udec(events);
    rv32_putc('\n');
}

int main(uint32_t hart, uintptr_t tree_address)
{
    fdt tree;
    uint32_t size;
    fdt_status status = fdt_open(&tree, tree_address);
    status = status != FDT_OK ? status : fdt_find(&tree, "compatible", "riscv,clint0", 0, &clint, &size);
    status = status != FDT_OK ? status : fdt_find(&tree, "compatible", "riscv,plic0", 0, &plic, &size);
    if (status != FDT_OK) {
        rv32_puts("irqcheck: FAILED no CLINT or PLIC, device tree status ");
        rv32_put_udec((uint32_t)status);
        rv32_putc('\n');
        return 1;
    }
    fold(hart);
    csr_write(CSR_MTVEC, (uint32_t)(uintptr_t)trap_entry);

    /* 1. The registers. */
    csr_write(CSR_MSCRATCH, 0x5a5aa5a5u);
    check("mscratch", csr_read(CSR_MSCRATCH), 0x5a5aa5a5u);
    csr_write(CSR_MIE, 0);
    csr_set(CSR_MSTATUS, MSTATUS_MIE | MSTATUS_MPIE);
    check("mstatus set", csr_read(CSR_MSTATUS) & (MSTATUS_MIE | MSTATUS_MPIE), MSTATUS_MIE | MSTATUS_MPIE);
    csr_clear(CSR_MSTATUS, MSTATUS_MIE | MSTATUS_MPIE);
    check("mstatus clear", csr_read(CSR_MSTATUS) & (MSTATUS_MIE | MSTATUS_MPIE), 0);
    csr_write(CSR_MIE, 0xffffffffu);
    check("mie set", csr_read(CSR_MIE) & (MIP_MSIP | MIP_MTIP | MIP_MEIP), MIP_MSIP | MIP_MTIP | MIP_MEIP);
    csr_write(CSR_MIE, 0);
    check("mie clear", csr_read(CSR_MIE) & (MIP_MSIP | MIP_MTIP | MIP_MEIP), 0);
    fold(csr_read(CSR_MSCRATCH));
    ok("registers");

    /* 2. Gating. */
    reset_counts();
    set_mtimecmp(0);
    check("MTIP pending", csr_read(CSR_MIP) & MIP_MTIP, MIP_MTIP);
    csr_set(CSR_MSTATUS, MSTATUS_MIE); /* enabled globally, not in mie */
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, MIP_MTIP);      /* in mie, not globally */
    check("nothing taken while disabled", ntaken, 0);
    csr_set(CSR_MSTATUS, MSTATUS_MIE);
    uint32_t after = csr_read(CSR_MSTATUS) & MSTATUS_MIE;
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, 0);
    check("timer taken", ntaken, 1);
    check("timer cause", taken[0], IRQ_MTI);
    check("handler mstatus", handler_status, MSTATUS_MPIE);
    check("mret restores MIE", after, MSTATUS_MIE);
    check("MTIP cleared", csr_read(CSR_MIP) & MIP_MTIP, 0);
    fold(ntaken);
    fold(taken[0]);
    fold(handler_status);
    ok("gating");

    /* 3. A software interrupt. */
    reset_counts();
    mmio_write32(clint + RV32_CLINT_MSIP, 1);
    check("MSIP pending", csr_read(CSR_MIP) & MIP_MSIP, MIP_MSIP);
    csr_write(CSR_MIE, MIP_MSIP);
    csr_set(CSR_MSTATUS, MSTATUS_MIE);
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, 0);
    check("software taken", ntaken, 1);
    check("software cause", taken[0], IRQ_MSI);
    fold(taken[0]);
    ok("software");

    /* 4. Priority: both pending, MSI first. */
    reset_counts();
    mmio_write32(clint + RV32_CLINT_MSIP, 1);
    set_mtimecmp(0);
    csr_write(CSR_MIE, MIP_MSIP | MIP_MTIP);
    csr_set(CSR_MSTATUS, MSTATUS_MIE);
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, 0);
    check("two taken", ntaken, 2);
    check("MSI first", taken[0], IRQ_MSI);
    check("MTI second", taken[1], IRQ_MTI);
    fold(taken[0]);
    fold(taken[1]);
    ok("priority");

    /* 5. wfi until a timer interrupt 2,000 ticks ahead. */
    reset_counts();
    uint64_t target = mtime() + 2000;
    set_mtimecmp(target);
    csr_write(CSR_MIE, MIP_MTIP);
    csr_set(CSR_MSTATUS, MSTATUS_MIE);
    while (ntaken == 0) {
        wait_for_interrupt();
    }
    csr_clear(CSR_MSTATUS, MSTATUS_MIE);
    csr_write(CSR_MIE, 0);
    check("wfi woke on the timer", taken[0], IRQ_MTI);
    check("mtime reached mtimecmp", mtime() >= target, 1);
    fold(taken[0]);
    ok("wfi");

    /* 6. The PLIC's registers, through the input's source number on every platform. */
    mmio_write32(plic + RV32_PLIC_PRIORITY(RV32_PLIC_SOURCE_INPUT), 5);
    mmio_write32(plic + RV32_PLIC_THRESHOLD, 3);
    mmio_write32(plic + RV32_PLIC_ENABLE, 1u << RV32_PLIC_SOURCE_INPUT);
    check("plic priority", mmio_read32(plic + RV32_PLIC_PRIORITY(RV32_PLIC_SOURCE_INPUT)), 5);
    check("plic threshold", mmio_read32(plic + RV32_PLIC_THRESHOLD), 3);
    check("plic enable", mmio_read32(plic + RV32_PLIC_ENABLE) & (1u << RV32_PLIC_SOURCE_INPUT), 1u << RV32_PLIC_SOURCE_INPUT);
    check("plic pending", mmio_read32(plic + RV32_PLIC_PENDING) & (1u << RV32_PLIC_SOURCE_INPUT), 0);
    check("plic claim", mmio_read32(plic + RV32_PLIC_CLAIM), 0);
    check("no MEIP", csr_read(CSR_MIP) & MIP_MEIP, 0);
    mmio_write32(plic + RV32_PLIC_THRESHOLD, 0);
    mmio_write32(plic + RV32_PLIC_ENABLE, 0);
    mmio_write32(plic + RV32_PLIC_PRIORITY(RV32_PLIC_SOURCE_INPUT), 0);
    fold(5);
    fold(3);
    ok("plic");

    /* 7. Our input device, where the tree lists it. */
    keyboard(&tree);

    if (failures) {
        return 1;
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
