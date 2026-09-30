/* virtio-mmio version 2 and virtio-blk as a driver sees them (Track 2, O3): the register
 * offsets, status and descriptor bits, and the split-queue structures for a queue of
 * VIRTIO_QUEUE entries. Shared by the kernel's driver (os/virtio.c) and virtiocheck.c. */
#ifndef RV32_VIRTIO_MMIO_H
#define RV32_VIRTIO_MMIO_H

#include <stdint.h>

#define VIRTIO_MAGIC_VALUE 0x74726976u /* "virt" */

#define VIRTIO_REG_MAGIC 0x000u
#define VIRTIO_REG_VERSION 0x004u
#define VIRTIO_REG_DEVICE 0x008u
#define VIRTIO_REG_DEVICE_FEATURES 0x010u
#define VIRTIO_REG_DEVICE_FEATURES_SEL 0x014u
#define VIRTIO_REG_DRIVER_FEATURES 0x020u
#define VIRTIO_REG_DRIVER_FEATURES_SEL 0x024u
#define VIRTIO_REG_QUEUE_SEL 0x030u
#define VIRTIO_REG_QUEUE_NUM_MAX 0x034u
#define VIRTIO_REG_QUEUE_NUM 0x038u
#define VIRTIO_REG_QUEUE_READY 0x044u
#define VIRTIO_REG_QUEUE_NOTIFY 0x050u
#define VIRTIO_REG_INTERRUPT_STATUS 0x060u
#define VIRTIO_REG_INTERRUPT_ACK 0x064u
#define VIRTIO_REG_STATUS 0x070u
#define VIRTIO_REG_QUEUE_DESC 0x080u   /* low word; the high word follows */
#define VIRTIO_REG_QUEUE_DRIVER 0x090u
#define VIRTIO_REG_QUEUE_DEVICE 0x0a0u
#define VIRTIO_REG_CONFIG_GENERATION 0x0fcu
#define VIRTIO_REG_CAPACITY 0x100u     /* virtio-blk's capacity in 512-byte sectors, low word */

#define VIRTIO_STATUS_ACKNOWLEDGE 1u
#define VIRTIO_STATUS_DRIVER 2u
#define VIRTIO_STATUS_DRIVER_OK 4u
#define VIRTIO_STATUS_FEATURES_OK 8u
#define VIRTIO_STATUS_NEEDS_RESET 0x40u
#define VIRTIO_DEVICE_BLOCK 2u

#define VIRTIO_DESC_NEXT 1u
#define VIRTIO_DESC_WRITE 2u /* the device writes this buffer */
#define VIRTIO_BLK_IN 0u     /* read */
#define VIRTIO_BLK_OUT 1u    /* write */
#define VIRTIO_BLK_OK 0u
#define VIRTIO_BLK_IOERR 1u
#define VIRTIO_BLK_UNSUPP 2u

#define VIRTIO_QUEUE 8u

struct virtio_desc {
    uint32_t addr, addr_hi, len;
    uint16_t flags, next;
};
struct virtio_avail {
    uint16_t flags, idx, ring[VIRTIO_QUEUE], used_event;
};
struct virtio_used {
    uint16_t flags, idx;
    struct {
        uint32_t id, len;
    } ring[VIRTIO_QUEUE];
    uint16_t avail_event;
};
struct virtio_blk_header {
    uint32_t type, reserved, sector, sector_hi;
};

#endif
