#ifndef RV32_SIMD4_DRIVER_H
#define RV32_SIMD4_DRIVER_H
#include <stdint.h>
#include <stdbool.h>
/* Word-indexed buffers, always transferred with 32-bit CPU accesses. */
enum simd4_result { SIMD4_OK, SIMD4_FAULT, SIMD4_TIMEOUT };
void simd4_reset(void);
bool simd4_load(const uint32_t program[256], const uint16_t data[256]);
bool simd4_start(unsigned entry);
enum simd4_result simd4_wait(unsigned polls);
bool simd4_read(unsigned index, uint16_t *value);
uint32_t simd4_counter(unsigned index); /* 0 cycles, 1 stalls, 2 transfers, 3 instructions */
#endif
