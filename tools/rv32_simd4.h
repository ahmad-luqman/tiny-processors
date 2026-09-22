/* Incremental four-lane device model; docs/rv32-simd4.md. */
#ifndef RV32_SIMD4_H
#define RV32_SIMD4_H
#include <stdbool.h>
#include <stdint.h>
#define SIMD_BASE 0x20004000u
#define SIMD_PROGRAM 0x20005000u
#define SIMD_DATA 0x20006000u
#define SIMD_COMMAND                 0x00
#define SIMD_STATUS                  0x04
#define SIMD_ENTRY                   0x08
#define SIMD_CYCLES                  0x0c
#define SIMD_STALLS                  0x10
#define SIMD_TRANSFERS               0x14
#define SIMD_INSTRUCTIONS            0x18
#define SIMD_BUSY                    0x01
#define SIMD_DONE                    0x02
#define SIMD_FAULT                   0x04
#define SIMD_START                   0x01
#define SIMD_RESET                   0x02

enum simd_phase { SIMD_IDLE, SIMD_FETCH, SIMD_EXECUTE, SIMD_MEMORY };

typedef struct {
    uint16_t r[4][4], loop;
    uint32_t acc[4], instruction;
    uint32_t cycles, stalls, transfers, instructions;
    uint8_t entry, pc, lane;
    enum simd_phase state;
    bool done, fault, command_tick;
} simd_execution;

typedef struct {
    uint32_t program[256];
    uint16_t data[256];
    simd_execution execution;
} simd_device;

/* Access does not advance time. False means a side-effect-free bus fault.
 * tick follows every CPU instruction; hold is a native-test memory delay. */
bool simd_access(simd_device *s, uint32_t address, int width, bool write, uint32_t *value);
void simd_tick(simd_device *s, bool hold);
void simd_reset(simd_device *s);
#endif
