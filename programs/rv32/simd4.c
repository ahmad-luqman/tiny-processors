#include "simd4.h"
#include "board.h"
#include "mmio.h"

void simd4_reset(void) { mmio_write32(RV32_SIMD4_BASE, 2); }

bool simd4_load(const uint32_t program[256], const uint16_t data[256])
{
    if (mmio_read32(RV32_SIMD4_BASE + 4) & 1) return false;
    for (unsigned i = 0; i < 256; i++) {
        mmio_write32(RV32_SIMD4_PROGRAM + 4 * i, program[i]);
        mmio_write32(RV32_SIMD4_DATA + 4 * i, data[i]);
    }
    return true;
}

bool simd4_start(unsigned entry)
{
    if (entry > 255 || (mmio_read32(RV32_SIMD4_BASE + 4) & 1)) return false;
    mmio_write32(RV32_SIMD4_BASE + 8, entry);
    mmio_write32(RV32_SIMD4_BASE, 1);
    return true;
}

enum simd4_result simd4_wait(unsigned polls)
{
    while (polls--) {
        uint32_t status = mmio_read32(RV32_SIMD4_BASE + 4);
        if (status & 2) return status & 4 ? SIMD4_FAULT : SIMD4_OK;
    }
    simd4_reset();
    return SIMD4_TIMEOUT;
}

bool simd4_read(unsigned index, uint16_t *value)
{
    if (index >= 256 || (mmio_read32(RV32_SIMD4_BASE + 4) & 1)) return false;
    *value = (uint16_t)mmio_read32(RV32_SIMD4_DATA + 4 * index);
    return true;
}

uint32_t simd4_counter(unsigned index)
{
    return index < 4 ? mmio_read32(RV32_SIMD4_BASE + 12 + index * 4) : 0;
}
