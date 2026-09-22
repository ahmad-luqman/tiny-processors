#include "simd4.h"
#include "board.h"
#include "mmio.h"

void simd4_reset(void) { mmio_write32(RV32_SIMD4_BASE + RV32_SIMD4_COMMAND, RV32_SIMD4_RESET); }

bool simd4_load(const uint32_t program[256], const uint16_t data[256])
{
    if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY) return false;
    for (unsigned i = 0; i < 256; i++) {
        mmio_write32(RV32_SIMD4_PROGRAM + 4 * i, program[i]);
        mmio_write32(RV32_SIMD4_DATA + 4 * i, data[i]);
    }
    return true;
}

bool simd4_start(unsigned entry)
{
    if (entry > 255 || (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY)) return false;
    mmio_write32(RV32_SIMD4_BASE + RV32_SIMD4_ENTRY, entry);
    mmio_write32(RV32_SIMD4_BASE + RV32_SIMD4_COMMAND, RV32_SIMD4_START);
    return true;
}

enum simd4_result simd4_wait(unsigned polls)
{
    while (polls--) {
        uint32_t status = mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS);
        if (status & RV32_SIMD4_DONE) return status & RV32_SIMD4_FAULT ? SIMD4_FAULT : SIMD4_OK;
    }
    simd4_reset();
    return SIMD4_TIMEOUT;
}

/* True when the caller owns the buffers: idle, and the block stays in range. */
static bool writable(unsigned index, unsigned count)
{
    return count <= 256 && index <= 256 - count &&
           !(mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY);
}

bool simd4_write(unsigned index, uint16_t value)
{
    if (!writable(index, 1)) return false;
    mmio_write32(RV32_SIMD4_DATA + 4 * index, value);
    return true;
}

bool simd4_write_bytes(unsigned index, const uint8_t *values, unsigned count)
{
    if (!writable(index, count)) return false;
    for (unsigned i = 0; i < count; i++) mmio_write32(RV32_SIMD4_DATA + 4 * (index + i), values[i]);
    return true;
}

bool simd4_write_signed(unsigned index, const int8_t *values, unsigned count)
{
    if (!writable(index, count)) return false;
    for (unsigned i = 0; i < count; i++)
        mmio_write32(RV32_SIMD4_DATA + 4 * (index + i), (uint32_t)(int32_t)values[i]);
    return true;
}

bool simd4_read(unsigned index, uint16_t *value)
{
    if (index >= 256 || (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY)) return false;
    *value = (uint16_t)mmio_read32(RV32_SIMD4_DATA + 4 * index);
    return true;
}
