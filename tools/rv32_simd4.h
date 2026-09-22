/* Incremental four-lane device model; docs/rv32-simd4.md. */
#ifndef RV32_SIMD4_H
#define RV32_SIMD4_H
#include <stdbool.h>
#include <stdint.h>
#define SIMD_BASE 0x20004000u
#define SIMD_PROGRAM 0x20005000u
#define SIMD_DATA 0x20006000u

typedef struct {
    uint32_t program[256];
    uint16_t data[256];
    uint16_t r[4][4], loop;
    uint32_t acc[4], instruction;
    uint32_t cycles, stalls, transfers, instructions;
    uint8_t entry, pc, state, lane;
    bool done, fault, command_tick;
} simd_device;

/* Access does not advance time. False means a side-effect-free bus fault.
 * tick follows every CPU instruction; hold is a native-test memory delay. */
bool simd_access(simd_device *s, uint32_t address, int width, bool write, uint32_t *value);
void simd_tick(simd_device *s, bool hold);
void simd_reset(simd_device *s);
#endif
