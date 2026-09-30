/* virtio-blk check (Track 2, O3): one image for QEMU's virt board (started
 * with a 128 KiB disk on virtio-mmio-bus.0), our emulator and the RTL.
 *
 * It scans the device tree's "virtio,mmio" nodes for a block device, sets it
 * up as the virtio 1.x specification's driver initialization describes, and
 * checks: a sector written and read back; two requests made available before
 * one notify both complete; a read past the end of the disk is IOERR; an
 * unknown request type (6) is UNSUPP; InterruptStatus and the device's PLIC
 * source report completion until acknowledged. Every value it folds into the
 * PASS word is one the virtio specification fixes, so all three platforms
 * print the same word. The disk's sector 3 is overwritten.
 *
 * On our machine (root compatible "tiny-processors,rv32-machine") it goes on
 * to what our contract adds and QEMU does differently: a buffer that is not
 * word aligned is IOERR, a chain the device cannot follow or a queue outside
 * RAM sets DEVICE_NEEDS_RESET and serves nothing until a reset, and a byte
 * access or a write to a read-only register is an access fault (a trap
 * handler counts them). None of it is folded into the PASS word.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "fdt.h"
#include "mmio.h"

static inline void csr_write_mtvec(uint32_t value)
{
    __asm__ volatile("csrw mtvec, %0" ::"r"(value));
}

#define QUEUE 8u
#define SECTOR 512u
#define STATUS_WAITING 0xffu

struct desc {
    uint32_t addr, addr_hi, len;
    uint16_t flags, next;
};
struct avail {
    uint16_t flags, idx, ring[QUEUE], used_event;
};
struct used {
    uint16_t flags, idx;
    struct {
        uint32_t id, len;
    } ring[QUEUE];
    uint16_t avail_event;
};
struct header {
    uint32_t type, reserved, sector, sector_hi;
};

static struct desc descs[6] __attribute__((aligned(16)));
static struct avail avail __attribute__((aligned(4)));
static volatile struct used used __attribute__((aligned(4)));
static struct header headers[2] __attribute__((aligned(16)));
static volatile uint8_t statuses[2];
static uint32_t out[SECTOR / 4], in[SECTOR / 4], second[SECTOR / 4];
static uint32_t base, plic, source, checksum = 2166136261u, failures;
static uint16_t seen;
static volatile uint32_t faults;
extern void trap_entry(void);

/* trap_entry.S's handler: count an access fault and step over the instruction. */
uint32_t trap_handler(uint32_t cause, uint32_t tval, uint32_t epc)
{
    (void)tval;
    if (cause != 5 && cause != 7) {
        rv32_puts("virtiocheck: FAILED unexpected trap\n");
        rv32_exit(90);
    }
    faults = faults + 1;
    return epc + 4;
}

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u;
}

static void check(const char *what, uint32_t got, uint32_t want)
{
    if (got != want) {
        rv32_puts("virtiocheck: FAILED ");
        rv32_puts(what);
        rv32_puts(" got ");
        rv32_put_hex32(got);
        rv32_puts(" want ");
        rv32_put_hex32(want);
        rv32_putc('\n');
        failures++;
    }
}

/* Chain `slot`'s three descriptors (0-2 or 3-5) for one request. */
static void prepare(uint32_t slot, uint32_t type, uint32_t sector, void *buffer, int device_writes)
{
    struct desc *d = &descs[3 * slot];
    headers[slot] = (struct header){type, 0, sector, 0};
    statuses[slot] = STATUS_WAITING;
    d[0] = (struct desc){(uint32_t)(uintptr_t)&headers[slot], 0, sizeof headers[slot], 1, (uint16_t)(3 * slot + 1)};
    d[1] = (struct desc){(uint32_t)(uintptr_t)buffer, 0, SECTOR, (uint16_t)(device_writes ? 3 : 1), (uint16_t)(3 * slot + 2)};
    d[2] = (struct desc){(uint32_t)(uintptr_t)&statuses[slot], 0, 1, 2, 0};
    avail.ring[avail.idx % QUEUE] = (uint16_t)(3 * slot);
    __asm__ volatile("fence" ::: "memory");
    avail.idx++;
    __asm__ volatile("fence" ::: "memory");
}

/* Notify and wait for `count` more used entries; our device has finished before the store
 * completes, QEMU's some time after. */
static void submit(uint32_t count)
{
    mmio_write32(base + 0x050, 0);
    while ((uint16_t)(used.idx - seen) < count) {
    }
    seen = (uint16_t)(seen + count);
}

static uint32_t one(uint32_t type, uint32_t sector, void *buffer, int device_writes)
{
    prepare(0, type, sector, buffer, device_writes);
    submit(1);
    return statuses[0];
}

/* Set the device up from scratch, as main does. */
static void set_up(void)
{
    mmio_write32(base + 0x070, 0);
    mmio_write32(base + 0x070, 3);
    mmio_write32(base + 0x024, 1);
    mmio_write32(base + 0x020, 1);
    mmio_write32(base + 0x070, 11);
    mmio_write32(base + 0x038, QUEUE);
    mmio_write32(base + 0x080, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + 0x090, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + 0x0a0, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + 0x044, 1);
    mmio_write32(base + 0x070, 15);
    avail.idx = 0;
    used.idx = 0;
    seen = 0;
}

static void ours(void)
{
    uint8_t *bytes = (uint8_t *)in;
    csr_write_mtvec((uint32_t)(uintptr_t)trap_entry);
    /* A misaligned buffer. */
    check("misaligned buffer", one(0, 0, bytes + 1, 1), 1);
    /* A chain whose data descriptor lacks NEXT: DEVICE_NEEDS_RESET, nothing served. */
    prepare(0, 0, 0, in, 1);
    descs[1].flags = 2;
    mmio_write32(base + 0x050, 0);
    check("broken chain needs reset", mmio_read32(base + 0x070) & 0x40u, 0x40);
    check("nothing served", used.idx, seen);
    set_up();
    check("a reset clears it", mmio_read32(base + 0x070), 15);
    check("served again", one(0, 3, in, 1), 0);
    /* A queue outside RAM. */
    mmio_write32(base + 0x070, 0);
    mmio_write32(base + 0x070, 3);
    mmio_write32(base + 0x070, 11);
    mmio_write32(base + 0x038, QUEUE);
    mmio_write32(base + 0x080, 0x10000000u);
    mmio_write32(base + 0x090, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + 0x0a0, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + 0x044, 1);
    mmio_write32(base + 0x070, 15);
    avail.idx = 1;
    mmio_write32(base + 0x050, 0);
    check("descriptors outside RAM", mmio_read32(base + 0x070) & 0x40u, 0x40);
    set_up();
    /* Register misuse: each an access fault. */
    faults = 0;
    (void)*(volatile uint8_t *)(uintptr_t)(base + 0x070);
    *(volatile uint32_t *)(uintptr_t)(base + 0x000) = 1;
    (void)*(volatile uint32_t *)(uintptr_t)(base + 0x050);
    (void)*(volatile uint32_t *)(uintptr_t)(base + 0x0f8);
    check("faults", faults, 4);
    /* A notify with the queue not ready does nothing, and does not wait. */
    mmio_write32(base + 0x044, 0);
    prepare(0, 0, 0, in, 1);
    mmio_write32(base + 0x050, 0);
    check("queue not ready", used.idx, seen);
    rv32_puts("virtiocheck: our refusals\n");
}

static int find_disk(const fdt *tree)
{
    for (uint32_t node = 0;; node++) {
        uint32_t size;
        fdt_status status = fdt_find_nth(tree, "compatible", "virtio,mmio", node, 0, &base, &size);
        if (status != FDT_OK) {
            return 0;
        }
        if (mmio_read32(base + 0x008) == 2) {
            /* The source is this node's `interrupts`: fdt_cell answers for the first node only, so
             * the check below trusts virt's layout, slot n at source n + 1. */
            source = 1 + (base - 0x10001000u) / 0x1000u;
            return 1;
        }
    }
}

int main(uint32_t hart, uintptr_t tree_address)
{
    (void)hart;
    fdt tree;
    uint32_t size;
    if (fdt_open(&tree, tree_address) != FDT_OK || fdt_find(&tree, "compatible", "riscv,plic0", 0, &plic, &size) != FDT_OK ||
        !find_disk(&tree)) {
        rv32_puts("virtiocheck: FAILED no PLIC or no block device\n");
        return 1;
    }
    check("magic", mmio_read32(base + 0x000), 0x74726976u);
    check("version", mmio_read32(base + 0x004), 2);
    check("capacity", mmio_read32(base + 0x100), 256);
    fold(mmio_read32(base + 0x004));
    fold(mmio_read32(base + 0x100));
    /* Initialization (virtio 1.x, "Device Initialization"). */
    mmio_write32(base + 0x070, 0);
    mmio_write32(base + 0x070, 1);
    mmio_write32(base + 0x070, 3);
    mmio_write32(base + 0x014, 1);
    check("VERSION_1 offered", mmio_read32(base + 0x010) & 1u, 1);
    mmio_write32(base + 0x024, 1);
    mmio_write32(base + 0x020, 1);
    mmio_write32(base + 0x024, 0);
    mmio_write32(base + 0x020, 0);
    mmio_write32(base + 0x070, 11);
    check("FEATURES_OK", mmio_read32(base + 0x070) & 8u, 8);
    mmio_write32(base + 0x030, 0);
    check("queue not ready", mmio_read32(base + 0x044), 0);
    check("queue size", mmio_read32(base + 0x034) >= QUEUE, 1);
    mmio_write32(base + 0x038, QUEUE);
    mmio_write32(base + 0x080, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + 0x084, 0);
    mmio_write32(base + 0x090, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + 0x094, 0);
    mmio_write32(base + 0x0a0, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + 0x0a4, 0);
    mmio_write32(base + 0x044, 1);
    mmio_write32(base + 0x070, 15);
    rv32_puts("virtiocheck: set up\n");

    /* A sector out and back. */
    for (uint32_t i = 0; i < SECTOR / 4; i++) {
        out[i] = 0x9e3779b9u * (i + 1);
    }
    check("write", one(1, 3, out, 0), 0);
    check("read", one(0, 3, in, 1), 0);
    uint32_t same = 1;
    for (uint32_t i = 0; i < SECTOR / 4; i++) {
        same &= in[i] == out[i];
        fold(in[i]);
    }
    check("read back", same, 1);
    check("used length of a read", used.ring[(seen - 1) % QUEUE].len, SECTOR + 1);
    rv32_puts("virtiocheck: sector 3 written and read\n");

    /* The interrupt: InterruptStatus and the PLIC's pending bit until acknowledged. */
    check("interrupt status", mmio_read32(base + 0x060) & 1u, 1);
    check("PLIC pending", (mmio_read32(plic + RV32_PLIC_PENDING) >> source) & 1u, 1);
    mmio_write32(base + 0x064, 1);
    check("acknowledged", mmio_read32(base + 0x060) & 1u, 0);
    check("PLIC quiet", (mmio_read32(plic + RV32_PLIC_PENDING) >> source) & 1u, 0);
    rv32_puts("virtiocheck: interrupt\n");

    /* Two requests, one notify. */
    prepare(0, 0, 3, second, 1);
    prepare(1, 0, 0, in, 1);
    submit(2);
    check("first of two", statuses[0], 0);
    check("second of two", statuses[1], 0);
    check("same sector again", second[17], out[17]);
    fold(statuses[0] | statuses[1] << 8);
    rv32_puts("virtiocheck: two requests\n");

    /* Refusals. */
    uint32_t past = one(0, 256, in, 1), unknown = one(6, 0, in, 1); /* no such type */
    check("past the end", past, 1);   /* VIRTIO_BLK_S_IOERR */
    check("unknown type", unknown, 2); /* VIRTIO_BLK_S_UNSUPP */
    fold(past);
    fold(unknown);
    rv32_puts("virtiocheck: refusals\n");
    mmio_write32(base + 0x064, mmio_read32(base + 0x060));
    const char *machine = "";
    (void)fdt_root_string(&tree, "compatible", &machine);
    if (fdt_same(machine, "tiny-processors,rv32-machine")) {
        ours();
    } else {
        rv32_puts("virtiocheck: our refusals skipped\n");
    }
    mmio_write32(base + 0x070, 0);
    if (failures) {
        return 1;
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
