/* Test-only inspection bridge. The production access/tick functions do all work. */
#include "rv32_simd4.h"
#include <string.h>
enum native_field { NATIVE_PC = 20, NATIVE_LOOP, NATIVE_PHASE, NATIVE_INSTRUCTION, NATIVE_LANE, NATIVE_COMMAND_TICK };
static simd_device device;
void native_init(void) { memset(&device, 0, sizeof device); simd_reset(&device); }
void native_reset(void) { simd_reset(&device); }
int native_access(uint32_t address, int width, int write, uint32_t *value)
{ return simd_access(&device, address, width, write != 0, value); }
void native_tick(int hold) { simd_tick(&device, hold != 0); }
uint32_t native_get(unsigned index)
{
    if (index < 16) return device.execution.r[index / 4][index % 4];
    if (index < 20) return device.execution.acc[index - 16];
    switch (index) {
    case NATIVE_PC: return device.execution.pc;
    case NATIVE_LOOP: return device.execution.loop;
    case NATIVE_PHASE: return device.execution.state;
    case NATIVE_INSTRUCTION: return device.execution.instruction;
    case NATIVE_LANE: return device.execution.lane;
    case NATIVE_COMMAND_TICK: return device.execution.command_tick;
    default: return 0;
    }
}
uint32_t native_data(unsigned index) { return device.data[index & 255]; }
