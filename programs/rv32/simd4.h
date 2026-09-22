#ifndef RV32_SIMD4_DRIVER_H
#define RV32_SIMD4_DRIVER_H
#include <stdint.h>
#include <stdbool.h>
#include "board.h"
#include "mmio.h"
/* Word-indexed buffers, always transferred with 32-bit CPU accesses. */
enum simd4_result { SIMD4_OK, SIMD4_FAULT, SIMD4_TIMEOUT };
void simd4_reset(void);
bool simd4_load(const uint32_t program[256], const uint16_t data[256]);
bool simd4_start(unsigned entry);
/* Poll at most `polls` times. Timeout resets the device (ENTRY/status/counters
 * are cleared; accepted stores survive). Zero means abort now, without polling. */
enum simd4_result simd4_wait(unsigned polls);
bool simd4_read(unsigned index, uint16_t *value);
/* 0 cycles, 1 stalls, 2 transfers, 3 instructions; false for an invalid index,
 * leaving *value unchanged. Caller supplies a valid output pointer. */
static inline bool simd4_counter(unsigned index, uint32_t *value)
{
    if (index >= 4) return false;
    *value = mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_CYCLES + index * 4);
    return true;
}
#endif
