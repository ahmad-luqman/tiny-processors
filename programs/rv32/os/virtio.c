/* The kernel's virtio-blk driver; see virtio.h and docs/rv32-os.md. */
#include "virtio.h"

#include "mmio.h"
#include "virtio_mmio.h"

#define QUEUE VIRTIO_QUEUE
#define WAIT_LIMIT 0x01000000u /* polls of the used ring before a request counts as lost */

static struct virtio_desc descs[3] __attribute__((aligned(16)));
static struct virtio_avail avail __attribute__((aligned(4)));
static struct virtio_used used __attribute__((aligned(4)));
static struct virtio_blk_header header __attribute__((aligned(16)));
static volatile uint8_t status_byte;
static uint32_t device;
static uint16_t seen;
static const char *failure;

const char *virtio_failure(void)
{
    return failure;
}

static int fail(const char *why)
{
    failure = why;
    device = 0;
    return 0;
}

uint32_t virtio_init(uint32_t base)
{
    if (mmio_read32(base + VIRTIO_REG_MAGIC) != VIRTIO_MAGIC_VALUE || mmio_read32(base + VIRTIO_REG_VERSION) != 2 ||
        mmio_read32(base + VIRTIO_REG_DEVICE) != VIRTIO_DEVICE_BLOCK) {
        return 0;
    }
    mmio_write32(base + VIRTIO_REG_STATUS, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, VIRTIO_STATUS_ACKNOWLEDGE);
    mmio_write32(base + VIRTIO_REG_STATUS, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER);
    mmio_write32(base + VIRTIO_REG_DEVICE_FEATURES_SEL, 1);
    if (!(mmio_read32(base + VIRTIO_REG_DEVICE_FEATURES) & 1u)) { /* VIRTIO_F_VERSION_1, feature bit 32 */
        return 0;
    }
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES_SEL, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES, 1);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES_SEL, 0);
    mmio_write32(base + VIRTIO_REG_DRIVER_FEATURES, 0);
    mmio_write32(base + VIRTIO_REG_STATUS, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK);
    if (!(mmio_read32(base + VIRTIO_REG_STATUS) & VIRTIO_STATUS_FEATURES_OK)) {
        return 0;
    }
    mmio_write32(base + VIRTIO_REG_QUEUE_SEL, 0);
    if (mmio_read32(base + VIRTIO_REG_QUEUE_READY) || mmio_read32(base + VIRTIO_REG_QUEUE_NUM_MAX) < QUEUE) {
        return 0;
    }
    mmio_write32(base + VIRTIO_REG_QUEUE_NUM, QUEUE);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + VIRTIO_REG_QUEUE_DESC + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + VIRTIO_REG_QUEUE_DRIVER + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + VIRTIO_REG_QUEUE_DEVICE + 4, 0);
    mmio_write32(base + VIRTIO_REG_QUEUE_READY, 1);
    mmio_write32(base + VIRTIO_REG_STATUS, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK);
    device = base;
    seen = 0;
    return mmio_read32(base + VIRTIO_REG_CAPACITY);
}

int virtio_transfer(uint32_t sector, void *buffer, uint32_t count, int write)
{
    if (!device) {
        return 0;
    }
    header.type = write ? VIRTIO_BLK_OUT : VIRTIO_BLK_IN;
    header.reserved = 0;
    header.sector = sector;
    header.sector_hi = 0;
    status_byte = 0xff;
    descs[0] = (struct virtio_desc){(uint32_t)(uintptr_t)&header, 0, sizeof header, VIRTIO_DESC_NEXT, 1};
    descs[1] = (struct virtio_desc){(uint32_t)(uintptr_t)buffer, 0, count * VIRTIO_SECTOR,
                                    (uint16_t)(VIRTIO_DESC_NEXT | (write ? 0u : VIRTIO_DESC_WRITE)), 2};
    descs[2] = (struct virtio_desc){(uint32_t)(uintptr_t)&status_byte, 0, 1, VIRTIO_DESC_WRITE, 0};
    avail.ring[avail.idx % QUEUE] = 0;
    __asm__ volatile("fence" ::: "memory"); /* the descriptors before the index */
    avail.idx++;
    __asm__ volatile("fence" ::: "memory");
    mmio_write32(device + VIRTIO_REG_QUEUE_NOTIFY, 0);
    /* Our device has finished when the notify completes; QEMU's finishes later. */
    for (uint32_t polls = 0; *(volatile uint16_t *)&used.idx == seen; polls++) {
        if (mmio_read32(device + VIRTIO_REG_STATUS) & VIRTIO_STATUS_NEEDS_RESET) {
            return fail("the device needs a reset");
        }
        if (polls == WAIT_LIMIT) {
            return fail("a request never completed");
        }
    }
    __asm__ volatile("fence" ::: "memory"); /* the used index before the status byte and the data */
    seen++;
    mmio_write32(device + VIRTIO_REG_INTERRUPT_ACK, mmio_read32(device + VIRTIO_REG_INTERRUPT_STATUS));
    return status_byte == 0;
}
