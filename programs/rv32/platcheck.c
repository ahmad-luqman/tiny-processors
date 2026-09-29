/* Platform check (Track 1): one image for QEMU's virt board, our emulator and
 * the RTL.
 *
 * start.S calls main without touching a0 or a1, so main receives the boot
 * convention's hart id and device-tree address (docs/rv32.md, "Boot
 * convention"). The program learns the platform only from that tree:
 *
 * 1. The devices virt and our machine share must be where the contract puts
 *    them: memory, the done register, the console and the CLINT.
 * 2. The CLINT works: mtime advances, the `time` CSR follows a write to
 *    mtime, and mtimecmp and msip hold what is written.
 * 3. Each of our own devices is used only if the tree lists it. On our
 *    machine (root compatible "tiny-processors,rv32-machine") all must be
 *    listed at their board.h addresses; on another platform an unlisted one
 *    is reported absent and never touched.
 *
 * The PASS word folds only what every backend shares (the four shared
 * addresses, the memory size and the CLINT results), so QEMU, the emulator
 * and the RTL print the same word; the device lines in between differ, and
 * the runner checks them per platform.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "fdt.h"
#include "gpu.h"
#include "mmio.h"

#define CSR_TIME 0xc01
#define CSR_TIMEH 0xc81
#define MACHINE "tiny-processors,rv32-machine"

static uint32_t checksum = 2166136261u;
static uint32_t failures;

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u; /* FNV-1a, as the self-check and the diagnostic */
}

static int same(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

static void fail(const char *what)
{
    rv32_puts("platcheck: FAILED ");
    rv32_puts(what);
    rv32_putc('\n');
    failures++;
}

static void line(const char *name, uint32_t a, uint32_t b, int two)
{
    rv32_puts("platcheck: ");
    rv32_puts(name);
    rv32_putc(' ');
    rv32_put_hex32(a);
    if (two) {
        rv32_putc(' ');
        rv32_put_hex32(b);
    }
    rv32_putc('\n');
}

/* The first node matching either compatible string, which must be at `expected`. */
static int shared_device(const fdt *t, const char *name, const char *ours, const char *theirs, uint32_t expected)
{
    uint32_t base = 0, size = 0;
    if (fdt_find(t, "compatible", ours, 0, &base, &size) != FDT_OK &&
        fdt_find(t, "compatible", theirs, 0, &base, &size) != FDT_OK) {
        fail(name);
        return 0;
    }
    line(name, base, 0, 0);
    if (base != expected) {
        fail(name);
        return 0;
    }
    fold(base);
    return 1;
}

static uint32_t clint(uint32_t offset)
{
    return mmio_read32(RV32_CLINT_BASE + offset);
}

static void set_clint(uint32_t offset, uint32_t value)
{
    mmio_write32(RV32_CLINT_BASE + offset, value);
}

static uint32_t read_timeh(void)
{
    uint32_t value;
    __asm__ volatile("csrr %0, %1" : "=r"(value) : "i"(CSR_TIMEH));
    return value;
}

static void check_clint(void)
{
    /* mtime advances: one tick is an instruction, a clock cycle or 100 ns, so wait for a change. */
    uint32_t start = clint(RV32_CLINT_MTIME), now = start, spins = 0;
    while (now == start && ++spins < 1000000u) {
        now = clint(RV32_CLINT_MTIME);
    }
    fold(now != start);
    if (now == start) {
        fail("mtime does not advance");
    } else {
        rv32_puts("platcheck: mtime advances\n");
    }

    /* time shadows mtime: set the high word; the low word starts at 0 so it cannot carry. */
    set_clint(RV32_CLINT_MTIME, 0);
    set_clint(RV32_CLINT_MTIME + 4, 5);
    uint32_t high = clint(RV32_CLINT_MTIME + 4), time_high = read_timeh();
    fold(high);
    fold(time_high);
    if (high != 5 || time_high != 5) {
        fail("time does not follow mtime");
    } else {
        rv32_puts("platcheck: time follows mtime\n");
    }

    /* mtimecmp and msip are plain registers until interrupts exist (O1). mtimecmp goes back to
     * all ones, above any count, and msip back to 0, so enabling interrupts later finds nothing
     * pending. */
    set_clint(RV32_CLINT_MTIMECMP, 0x89abcdefu);
    set_clint(RV32_CLINT_MTIMECMP + 4, 0x01234567u);
    set_clint(RV32_CLINT_MSIP, 1);
    uint32_t cmp_low = clint(RV32_CLINT_MTIMECMP), cmp_high = clint(RV32_CLINT_MTIMECMP + 4);
    uint32_t msip = clint(RV32_CLINT_MSIP);
    set_clint(RV32_CLINT_MSIP, 0);
    set_clint(RV32_CLINT_MTIMECMP + 4, 0xffffffffu);
    set_clint(RV32_CLINT_MTIMECMP, 0xffffffffu);
    fold(cmp_low);
    fold(cmp_high);
    fold(msip);
    if (cmp_low != 0x89abcdefu || cmp_high != 0x01234567u || msip != 1 || clint(RV32_CLINT_MSIP) != 0) {
        fail("mtimecmp or msip");
    } else {
        rv32_puts("platcheck: mtimecmp and msip hold\n");
    }
}

/* One of our devices: absent is fine off our machine; present means at board.h's address. */
static int own_device(const fdt *t, int ours, const char *name, const char *compatible,
                      uint32_t index, uint32_t expected)
{
    uint32_t base = 0, size = 0;
    if (fdt_find(t, "compatible", compatible, index, &base, &size) != FDT_OK) {
        rv32_puts("platcheck: ");
        rv32_puts(name);
        rv32_puts(" absent\n");
        if (ours) {
            fail(name);
        }
        return 0;
    }
    line(name, base, size, 1);
    if (base != expected) {
        fail(name);
        return 0;
    }
    return 1;
}

static void check_own_devices(const fdt *t, int ours)
{
    if (own_device(t, ours, "input", "tiny-processors,input", 0, RV32_INPUT_BASE) &&
        mmio_read32(RV32_INPUT_BASE + RV32_INPUT_COUNT) > RV32_INPUT_QUEUE) {
        fail("input COUNT");
    }
    if (own_device(t, ours, "display", "tiny-processors,display", 0, RV32_DISPLAY_BASE)) {
        if (mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_WIDTH) != RV32_DISPLAY_COLUMNS ||
            mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_HEIGHT) != RV32_DISPLAY_ROWS) {
            fail("display size");
        }
        own_device(t, ours, "framebuffer", "tiny-processors,display", 1, RV32_FB_BASE);
    }
    if (own_device(t, ours, "simd4", "tiny-processors,simd4", 0, RV32_SIMD4_BASE)) {
        own_device(t, ours, "simd4-program", "tiny-processors,simd4", 1, RV32_SIMD4_PROGRAM);
        own_device(t, ours, "simd4-data", "tiny-processors,simd4", 2, RV32_SIMD4_DATA);
        if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY) {
            fail("simd4 busy at boot");
        }
    }
    if (own_device(t, ours, "g1", "tiny-processors,g1", 0, RV32_GPU_BASE) &&
        (mmio_read32(RV32_GPU_BASE + GPU_STATUS) & GPU_BUSY)) {
        fail("g1 busy at boot");
    }
    own_device(t, ours, "g2", "tiny-processors,g2", 0, RV32_G3D_BASE);
}

int main(uint32_t hart, uintptr_t tree)
{
    fdt t;
    rv32_puts("platcheck: hart ");
    rv32_put_udec(hart);
    rv32_putc('\n');
    fold(hart);
    int status = fdt_open(&t, tree);
    if (status != FDT_OK) {
        rv32_puts("platcheck: no device tree in a1, error ");
        rv32_put_udec((uint32_t)status);
        rv32_puts("\nFAIL 1\n");
        return 1;
    }
    const char *model = fdt_root_string(&t, "model");
    rv32_puts("platcheck: model ");
    rv32_puts(model ? model : "(none)");
    rv32_putc('\n');
    const char *machine = fdt_root_string(&t, "compatible"); /* the first entry of the list */
    int ours = machine && same(machine, MACHINE);
    uint32_t base = 0, size = 0;

    if (fdt_find(&t, "device_type", "memory", 0, &base, &size) != FDT_OK) {
        fail("memory");
    } else {
        line("memory", base, size, 1);
        fold(base);
        fold(size);
        if (base != RV32_RAM_BASE || size != RV32_RAM_PLANNED_SIZE) {
            fail("memory");
        }
    }
    int shared = shared_device(&t, "done", "tiny-processors,done", "sifive,test0", RV32_DONE);
    shared &= shared_device(&t, "console", "tiny-processors,console", "ns16550a", RV32_CONSOLE_BASE);
    if (shared_device(&t, "clint", "tiny-processors,clint", "riscv,clint0", RV32_CLINT_BASE)) {
        check_clint();
    }
    check_own_devices(&t, ours);

    if (failures || !shared) {
        rv32_puts("FAIL 2\n");
        return 2;
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
