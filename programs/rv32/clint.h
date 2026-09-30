/* The CLINT's 64-bit time registers from C (Track 2): read mtime without a torn carry, and set
 * mtimecmp without passing through a value below the target. Shared by irqcheck and the kernel,
 * which find the CLINT's base in the device tree. */
#ifndef RV32_CLINT_H
#define RV32_CLINT_H

#include <stdint.h>

#include "board.h"
#include "mmio.h"

static inline uint64_t clint_mtime(uint32_t clint)
{
    uint32_t hi, lo;
    do {
        hi = mmio_read32(clint + RV32_CLINT_MTIME + 4);
        lo = mmio_read32(clint + RV32_CLINT_MTIME);
    } while (hi != mmio_read32(clint + RV32_CLINT_MTIME + 4));
    return (uint64_t)hi << 32 | lo;
}

static inline void clint_set_mtimecmp(uint32_t clint, uint64_t when)
{
    /* Low word to all ones first, so no intermediate value lies below the target. */
    mmio_write32(clint + RV32_CLINT_MTIMECMP, 0xffffffffu);
    mmio_write32(clint + RV32_CLINT_MTIMECMP + 4, (uint32_t)(when >> 32));
    mmio_write32(clint + RV32_CLINT_MTIMECMP, (uint32_t)when);
}

#endif
