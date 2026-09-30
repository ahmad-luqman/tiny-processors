/* The kernel's virtio-blk driver; see virtio.h and docs/rv32-os.md. */
#include "virtio.h"

#include "mmio.h"

#define MAGIC 0x74726976u
#define REG_MAGIC 0x000u
#define REG_VERSION 0x004u
#define REG_DEVICE 0x008u
#define REG_DEVICE_FEATURES 0x010u
#define REG_DEVICE_FEATURES_SEL 0x014u
#define REG_DRIVER_FEATURES 0x020u
#define REG_DRIVER_FEATURES_SEL 0x024u
#define REG_QUEUE_SEL 0x030u
#define REG_QUEUE_NUM_MAX 0x034u
#define REG_QUEUE_NUM 0x038u
#define REG_QUEUE_READY 0x044u
#define REG_QUEUE_NOTIFY 0x050u
#define REG_INTERRUPT_STATUS 0x060u
#define REG_INTERRUPT_ACK 0x064u
#define REG_STATUS 0x070u
#define REG_QUEUE_DESC 0x080u
#define REG_QUEUE_DRIVER 0x090u
#define REG_QUEUE_DEVICE 0x0a0u
#define REG_CAPACITY 0x100u
#define STATUS_ACKNOWLEDGE 1u
#define STATUS_DRIVER 2u
#define STATUS_DRIVER_OK 4u
#define STATUS_FEATURES_OK 8u
#define DEVICE_BLOCK 2u
#define QUEUE 8u
#define DESC_NEXT 1u
#define DESC_WRITE 2u
#define TYPE_IN 0u
#define TYPE_OUT 1u

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

static struct desc descs[3] __attribute__((aligned(16)));
static struct avail avail __attribute__((aligned(4)));
static struct used used __attribute__((aligned(4)));
static struct header header __attribute__((aligned(16)));
static volatile uint8_t status_byte;
static uint32_t device;
static uint16_t seen;

uint32_t virtio_init(uint32_t base)
{
    if (mmio_read32(base + REG_MAGIC) != MAGIC || mmio_read32(base + REG_VERSION) != 2 ||
        mmio_read32(base + REG_DEVICE) != DEVICE_BLOCK) {
        return 0;
    }
    mmio_write32(base + REG_STATUS, 0);
    mmio_write32(base + REG_STATUS, STATUS_ACKNOWLEDGE);
    mmio_write32(base + REG_STATUS, STATUS_ACKNOWLEDGE | STATUS_DRIVER);
    mmio_write32(base + REG_DEVICE_FEATURES_SEL, 1);
    if (!(mmio_read32(base + REG_DEVICE_FEATURES) & 1u)) { /* VIRTIO_F_VERSION_1, feature bit 32 */
        return 0;
    }
    mmio_write32(base + REG_DRIVER_FEATURES_SEL, 1);
    mmio_write32(base + REG_DRIVER_FEATURES, 1);
    mmio_write32(base + REG_DRIVER_FEATURES_SEL, 0);
    mmio_write32(base + REG_DRIVER_FEATURES, 0);
    mmio_write32(base + REG_STATUS, STATUS_ACKNOWLEDGE | STATUS_DRIVER | STATUS_FEATURES_OK);
    if (!(mmio_read32(base + REG_STATUS) & STATUS_FEATURES_OK)) {
        return 0;
    }
    mmio_write32(base + REG_QUEUE_SEL, 0);
    if (mmio_read32(base + REG_QUEUE_READY) || mmio_read32(base + REG_QUEUE_NUM_MAX) < QUEUE) {
        return 0;
    }
    mmio_write32(base + REG_QUEUE_NUM, QUEUE);
    mmio_write32(base + REG_QUEUE_DESC, (uint32_t)(uintptr_t)descs);
    mmio_write32(base + REG_QUEUE_DESC + 4, 0);
    mmio_write32(base + REG_QUEUE_DRIVER, (uint32_t)(uintptr_t)&avail);
    mmio_write32(base + REG_QUEUE_DRIVER + 4, 0);
    mmio_write32(base + REG_QUEUE_DEVICE, (uint32_t)(uintptr_t)&used);
    mmio_write32(base + REG_QUEUE_DEVICE + 4, 0);
    mmio_write32(base + REG_QUEUE_READY, 1);
    mmio_write32(base + REG_STATUS, STATUS_ACKNOWLEDGE | STATUS_DRIVER | STATUS_FEATURES_OK | STATUS_DRIVER_OK);
    device = base;
    seen = 0;
    return mmio_read32(base + REG_CAPACITY);
}

int virtio_transfer(uint32_t sector, void *buffer, uint32_t count, int write)
{
    if (!device) {
        return 0;
    }
    header.type = write ? TYPE_OUT : TYPE_IN;
    header.reserved = 0;
    header.sector = sector;
    header.sector_hi = 0;
    status_byte = 0xff;
    descs[0] = (struct desc){(uint32_t)(uintptr_t)&header, 0, sizeof header, DESC_NEXT, 1};
    descs[1] = (struct desc){(uint32_t)(uintptr_t)buffer, 0, count * VIRTIO_SECTOR,
                             (uint16_t)(DESC_NEXT | (write ? 0u : DESC_WRITE)), 2};
    descs[2] = (struct desc){(uint32_t)(uintptr_t)&status_byte, 0, 1, DESC_WRITE, 0};
    avail.ring[avail.idx % QUEUE] = 0;
    __asm__ volatile("fence" ::: "memory"); /* the descriptors before the index */
    avail.idx++;
    __asm__ volatile("fence" ::: "memory");
    mmio_write32(device + REG_QUEUE_NOTIFY, 0);
    /* Our device has finished when the notify completes; QEMU's finishes later. */
    while (*(volatile uint16_t *)&used.idx == seen) {
        if (mmio_read32(device + REG_STATUS) & 0x40u) { /* DEVICE_NEEDS_RESET: the request was refused */
            device = 0;
            return 0;
        }
    }
    seen++;
    mmio_write32(device + REG_INTERRUPT_ACK, mmio_read32(device + REG_INTERRUPT_STATUS));
    return status_byte == 0;
}
