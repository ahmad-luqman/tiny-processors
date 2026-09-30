/* virtio-blk check (Track 2, O3): one image for QEMU's virt board (started
 * with a 128 KiB disk on virtio-mmio-bus.0), our emulator and the RTL.
 *
 * It scans the device tree's "virtio,mmio" nodes for a block device, sets it
 * up as the virtio 1.x specification's driver initialization describes, and
 * checks: a sector written and read back; two requests made available before
 * one notify both complete; a read past the end of the disk is IOERR, and so
 * is a two-sector write from the last sector, which leaves that sector as it
 * was; an unknown request type (6) is UNSUPP; InterruptStatus reports
 * completion until acknowledged, and the device's PLIC source is pending until
 * claimed. Every value it folds into the PASS word is one the virtio and PLIC
 * specifications fix, so all three platforms print the same word. The disk's sector 3 is overwritten.
 *
 * On our machine (root compatible "tiny-processors,rv32-machine") it goes on
 * to what our contract adds and QEMU does differently: a buffer that is not
 * word aligned is IOERR, a chain the device cannot follow (one naming a
 * descriptor past the queue's size included) or a queue outside RAM sets
 * DEVICE_NEEDS_RESET and serves nothing until a reset, and a byte
 * access or a write to a read-only register is an access fault (a trap
 * handler counts them). None of it is folded into the PASS word.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "fdt.h"
#include "mmio.h"
#include "virtio_mmio.h"

static inline void csr_write_mtvec(uint32_t value)
{
    __asm__ volatile("csrw mtvec, %0" ::"r"(value));
}

#define QUEUE VIRTIO_QUEUE
#define SECTOR 512u
#define STATUS_WAITING 0xffu

static struct virtio_desc descs[6] __attribute__((aligned(16)));
static struct virtio_avail avail __attribute__((aligned(4)));
static volatile struct virtio_used used __attribute__((aligned(4)));
static struct virtio_blk_header headers[2] __attribute__((aligned(16)));
static volatile uint8_t statuses[2];
static uint32_t out[SECTOR / 4], in[SECTOR / 4], second[SECTOR / 4], two[2 * SECTOR / 4];
static uint32_t base, plic, source, checksum = 2166136261u, failures;
static uint16_t seen;
static volatile uint32_t faults;
extern void trap_entry(void);

/* trap.S's handler: count an access fault and step over the instruction. */
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

/* Chain `slot`'s three descriptors (0-2 or 3-5) for one request of `length` bytes. */
static void prepare_length(uint32_t slot, uint32_t type, uint32_t sector, void *buffer, uint32_t length, int device_writes)
{
    struct virtio_desc *d = &descs[3 * slot];
    headers[slot] = (struct virtio_blk_header){type, 0, sector, 0};
    statuses[slot] = STATUS_WAITING;
    d[0] = (struct virtio_desc){(uint32_t)(uintptr_t)&headers[slot], 0, sizeof headers[slot], VIRTIO_DESC_NEXT, (uint16_t)(3 * slot + 1)};
    d[1] = (struct virtio_desc){(uint32_t)(uintptr_t)buffer, 0, length,
                                (uint16_t)(VIRTIO_DESC_NEXT | (device_writes ? VIRTIO_DESC_WRITE : 0u)), (uint16_t)(3 * slot + 2)};
    d[2] = (struct virtio_desc){(uint32_t)(uintptr_t)&statuses[slot], 0, 1, VIRTIO_DESC_WRITE, 0};
    avail.ring[avail.idx % QUEUE] = (uint16_t)(3 * slot);
    __asm__ volatile("fence" ::: "memory");
    avail.idx++;
    __asm__ volatile("fence" ::: "memory");
}

static void prepare(uint32_t slot, uint32_t type, uint32_t sector, void *buffer, int device_writes)
{
    prepare_length(slot, type, sector, buffer, SECTOR, device_writes);
}

/* Notify and wait for `count` more used entries; our device has finished before the store
 * completes, QEMU's some time after. */
static void submit(uint32_t count)
{
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
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
    mmio_write32(base + VIRTIO_REG_STATUS, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, 3);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES_SEL, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, 11);
    mmio_write32(base + VIRTIO_REG_QUEUE_NUM, QUEUE);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + VIRTIO_REG_QUEUE_READY, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, 15);
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
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
    check("broken chain needs reset", mmio_read32(base + VIRTIO_REG_STATUS) & 0x40u, 0x40);
    check("nothing served", used.idx, seen);
    set_up();
    check("a reset clears it", mmio_read32(base + VIRTIO_REG_STATUS), 15);
    check("served again", one(0, 3, in, 1), 0);
    /* A chain naming a descriptor past the queue's size, as a head or as a next: also a reset. */
    prepare(0, 0, 0, in, 1);
    descs[0].next = QUEUE;
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
    check("next past the queue", mmio_read32(base + VIRTIO_REG_STATUS) & VIRTIO_STATUS_NEEDS_RESET, VIRTIO_STATUS_NEEDS_RESET);
    set_up();
    prepare(0, 0, 0, in, 1);
    avail.ring[0] = QUEUE;
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
    check("head past the queue", mmio_read32(base + VIRTIO_REG_STATUS) & VIRTIO_STATUS_NEEDS_RESET, VIRTIO_STATUS_NEEDS_RESET);
    check("nothing served either", used.idx, seen);
    set_up();
    /* A queue outside RAM. */
    mmio_write32(base + VIRTIO_REG_STATUS, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, 3);
    mmio_write32(base + VIRTIO_REG_STATUS, 11);
    mmio_write32(base + VIRTIO_REG_QUEUE_NUM, QUEUE);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC, 0x10000000u);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + VIRTIO_REG_QUEUE_READY, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, 15);
    avail.idx = 1;
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
    check("descriptors outside RAM", mmio_read32(base + VIRTIO_REG_STATUS) & 0x40u, 0x40);
    set_up();
    /* Register misuse: each an access fault. */
    faults = 0;
    (void)*(volatile uint8_t *)(uintptr_t)(base + VIRTIO_REG_STATUS);
    *(volatile uint32_t *)(uintptr_t)(base + VIRTIO_REG_MAGIC) = 1;
    (void)*(volatile uint32_t *)(uintptr_t)(base + VIRTIO_REG_QUEUE_NOTIFY);
    (void)*(volatile uint32_t *)(uintptr_t)(base + 0x0f8); /* no register there */
    check("faults", faults, 4);
    /* A notify with the queue not ready does nothing, and does not wait. */
    mmio_write32(base + VIRTIO_REG_QUEUE_READY, 0);
    prepare(0, 0, 0, in, 1);
    mmio_write32(base + VIRTIO_REG_QUEUE_NOTIFY, 0);
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
        if (mmio_read32(base + VIRTIO_REG_DEVICE) == 2) {
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
    check("magic", mmio_read32(base + VIRTIO_REG_MAGIC), VIRTIO_MAGIC_VALUE);
    check("version", mmio_read32(base + VIRTIO_REG_VERSION), 2);
    check("capacity", mmio_read32(base + VIRTIO_REG_CAPACITY), 256);
    fold(mmio_read32(base + VIRTIO_REG_VERSION));
    fold(mmio_read32(base + VIRTIO_REG_CAPACITY));
    /* Initialization (virtio 1.x, "Device Initialization"). */
    mmio_write32(base + VIRTIO_REG_STATUS, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, 3);
    mmio_write32(base + VIRTIO_REG_DEVICE_FEATURES_SEL, 1);
    check("VERSION_1 offered", mmio_read32(base + VIRTIO_REG_DEVICE_FEATURES) & 1u, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES_SEL, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES_SEL, 0);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, 11);
    check("FEATURES_OK", mmio_read32(base + VIRTIO_REG_STATUS) & 8u, 8);
    mmio_write32(base + VIRTIO_REG_QUEUE_SEL, 0);
    check("queue not ready", mmio_read32(base + VIRTIO_REG_QUEUE_READY), 0);
    check("queue size", mmio_read32(base + VIRTIO_REG_QUEUE_NUM_MAX) >= QUEUE, 1);
    mmio_write32(base + VIRTIO_REG_QUEUE_NUM, QUEUE);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_READY, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, 15);
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

    /* The interrupt: InterruptStatus, and the PLIC's pending bit, which a claim clears (the gateway
     * holds a request until it is claimed, whatever the line does meanwhile). Acknowledged at the
     * device and completed at the PLIC, the source stays quiet. */
    check("interrupt status", mmio_read32(base + VIRTIO_REG_INTERRUPT_STATUS) & 1u, 1);
    check("PLIC pending", (mmio_read32(plic + RV32_PLIC_PENDING) >> source) & 1u, 1);
    mmio_write32(plic + RV32_PLIC_PRIORITY(source), 1);
    mmio_write32(plic + RV32_PLIC_THRESHOLD, 0);
    mmio_write32(plic + RV32_PLIC_ENABLE, 1u << source);
    uint32_t claimed = mmio_read32(plic + RV32_PLIC_CLAIM);
    check("claim", claimed, source);
    check("claimed, not pending", (mmio_read32(plic + RV32_PLIC_PENDING) >> source) & 1u, 0);
    mmio_write32(base + VIRTIO_REG_INTERRUPT_ACK, 1);
    check("acknowledged", mmio_read32(base + VIRTIO_REG_INTERRUPT_STATUS) & 1u, 0);
    mmio_write32(plic + RV32_PLIC_CLAIM, claimed);
    check("PLIC quiet", (mmio_read32(plic + RV32_PLIC_PENDING) >> source) & 1u, 0);
    check("nothing to claim", mmio_read32(plic + RV32_PLIC_CLAIM), 0);
    mmio_write32(plic + RV32_PLIC_ENABLE, 0);
    fold(claimed);
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
    check("past the end", past, VIRTIO_BLK_IOERR);
    check("unknown type", unknown, VIRTIO_BLK_UNSUPP);
    fold(past);
    fold(unknown);
    /* Two sectors from the last one: the request crosses the end, so none of it is written. */
    for (uint32_t i = 0; i < 2 * SECTOR / 4; i++) {
        two[i] = 0xa5a5a5a5u ^ i;
    }
    prepare_length(0, VIRTIO_BLK_OUT, 255, two, 2 * SECTOR, 0);
    submit(1);
    uint32_t crossing = statuses[0], untouched = one(VIRTIO_BLK_IN, 255, in, 1) == VIRTIO_BLK_OK;
    for (uint32_t i = 0; i < SECTOR / 4; i++) {
        untouched &= in[i] == 0; /* the disk starts blank */
    }
    check("across the end", crossing, VIRTIO_BLK_IOERR);
    check("last sector untouched", untouched, 1);
    fold(crossing);
    fold(untouched);
    rv32_puts("virtiocheck: refusals\n");
    mmio_write32(base + VIRTIO_REG_INTERRUPT_ACK, mmio_read32(base + VIRTIO_REG_INTERRUPT_STATUS));
    const char *machine = "";
    (void)fdt_root_string(&tree, "compatible", &machine);
    if (fdt_same(machine, "tiny-processors,rv32-machine")) {
        ours();
    } else {
        rv32_puts("virtiocheck: our refusals skipped\n");
    }
    mmio_write32(base + VIRTIO_REG_STATUS, 0);
    if (failures) {
        return 1;
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
