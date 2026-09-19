/* Memory-mapped device access. Every access is volatile so the compiler
 * performs exactly the loads and stores written, in order, at the width
 * written. */
#ifndef RV32_MMIO_H
#define RV32_MMIO_H

#include <stdint.h>

static inline void mmio_write8(uintptr_t address, uint8_t value)
{
    *(volatile uint8_t *)address = value;
}

static inline uint8_t mmio_read8(uintptr_t address)
{
    return *(volatile uint8_t *)address;
}

static inline void mmio_write32(uintptr_t address, uint32_t value)
{
    *(volatile uint32_t *)address = value;
}

#endif
