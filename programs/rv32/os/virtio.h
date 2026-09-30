/* The kernel's virtio-blk driver (Track 2, O3): virtio-mmio version 2, one
 * queue, requests one at a time, polled. The same driver serves our device and
 * QEMU's (started with -global virtio-mmio.force-legacy=false). */
#ifndef RV32_OS_VIRTIO_H
#define RV32_OS_VIRTIO_H

#include <stdint.h>

#define VIRTIO_SECTOR 512u

/* Set the device at `base` up; returns its capacity in sectors, or 0 when there is no usable
 * block device there (another device type, a legacy transport, a refused feature). */
uint32_t virtio_init(uint32_t base);
/* Read or write `count` whole sectors from `sector` into or out of `buffer` (word aligned);
 * returns 1 on success. */
int virtio_transfer(uint32_t sector, void *buffer, uint32_t count, int write);
/* Why the device stopped being used, or 0: after the device asks for a reset or a request never
 * completes, every later transfer fails at once. */
const char *virtio_failure(void);

#endif
